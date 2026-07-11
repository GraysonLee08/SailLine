# backend/app/services/routing/pipeline.py
"""Single source of truth for route compute orchestration.

Before this module existed, ``app/routers/routing.py`` (the synchronous
HTTP endpoint) and ``workers/route_recompute.py`` (the background
"better route" detector) each reimplemented ~70% of the same dance:
region resolution from marks centroid, optional currents loading, cache
key construction, forecast loading, navigability predicate, engine
invocation, GeoJSON assembly.

The drift was real and harmful:

* Endpoint passed ``duration_hours=payload.duration_hours`` to the
  forecast and currents loaders; the worker omitted it and got the
  loader defaults — different windows, different cycle bracketing.
* Endpoint accepted user-supplied ``max_tws_kt`` / ``polar_margin`` /
  ``hs_m`` / ``density_factor``; the worker hardcoded its own values.
* Endpoint downgraded free-tier callers to the GENERIC polar; the
  worker ran whatever polar the race's ``boat_class`` resolved to.

Net effect: the worker's "better route" alert could be computed under
a meaningfully different physics than the user-facing route, so a
"save 12 minutes" popup might evaporate the moment the user pressed
Recompute and the endpoint applied the real knobs.

What the pipeline does
----------------------
``compute_route(req, *, redis, use_cache)`` takes a fully-resolved
:class:`RouteRequest` (race-bound fields + user-tunable knobs already
populated by the caller) and runs the whole orchestration end-to-end.
Both the router and the worker become thin transport adapters:

* Router: builds a :class:`RouteRequest` from the pydantic body +
  authenticated tier resolution + race row, calls ``compute_route``
  with ``use_cache=True``, catches :class:`ForecastNotAvailable` and
  maps it to HTTP 425. After a successful compute, persists the
  request via :func:`save_last_request` so the worker can faithfully
  replay it.
* Worker: reads the last request via :func:`load_last_request`,
  overlays the canonical race fields (marks, start_at, boat_class)
  from the DB onto a fresh :class:`RouteRequest`, calls
  ``compute_route`` with ``use_cache=False`` (the worker always wants
  a fresh number to compare against the baseline). Silently skips on
  :class:`ForecastNotAvailable`.

Caller separation of concerns
-----------------------------
The pipeline does NOT own:

* Auth — the router decides whether the caller is allowed to compute
  this race and which polar tier applies. The worker doesn't have a
  user at all.
* HTTP transport — the pipeline raises a plain Python exception for
  the not-yet-available case; the router maps to 425.
* Notification state — ``route:last_best``, ``route:alternative``,
  the pub/sub channel all live in the worker. They're notification
  concerns, not routing concerns.

The pipeline DOES own the route cache (the ``route:{engine_version}:…``
key family). It's a function-of-inputs cache and lives with the
function whose inputs it keys on.

ENGINE_VERSION
--------------
Lives in this module — bumped whenever any field is added to
:class:`RouteRequest` or :class:`DeratingProfile`, or whenever the
engine's pruning / scoring behaviour changes. The pinning test in
``tests/test_engine_version.py`` snapshots the field set and fails if
a knob is added without a corresponding bump.

v11-pipeline: introduced this module; replaces v10-currents.
v12-fullrace: engine iteration budget sized to the race instead of the
fixed 240-iteration (20h-at-5min) per-leg default that truncated long
courses mid-lake; persist-last-frame wind beyond the forecast horizon
(``horizon_exceeded`` meta); ``reached=False`` results are no longer
cached.
v13-maneuver: tack/gybe penalties in the engine (a maneuvering step
covers less ground), top-2 frontier per bearing bin, tie-break against
maneuvered candidates. Fixes the staircase artifact where the culling
preferred one-step-per-gybe lineages hugging the direct bearing —
modeled time was identical, so the engine was indifferent; real boats
are not. No RouteRequest/DeratingProfile field changes.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from app.currents_regions import sources_covering_marks
from app.regions import REGIONS, base_region_for_point, venue_for_point
from app.services.bathymetry import BathymetryUnavailable
from app.services.boats import spec_for_class
from app.services.currents import (
    CurrentForecast,
    CurrentsUnavailable,
    load_currents_for_race,
)
from app.services.polars import load_polar
from app.services.redis_keys import (
    ROUTE_CACHE_TTL_S,
    ROUTE_LAST_REQUEST_TTL_S,
    route_cache_key,
    route_last_request_key,
)
from app.services.routing.isochrone import (
    compute_isochrone_route_multileg,
    haversine_m,
    route_to_geojson,
)
from app.services.routing.navigability import (
    DEFAULT_SAFETY_FACTOR,
    make_navigable_predicate,
)
# NB: `from app.services.weather import load_forecast_for_race` is imported
# LAZILY inside compute_route (below), not at module scope, to break a
# circular import. The worker entrypoint workers/race_postprocess.py
# imports app.services.weather FIRST; weather/__init__ → forecast_loader →
# routing.isochrone → routing/__init__ → routing.pipeline. A module-level
# import of weather here closes that cycle while weather is only partially
# initialised → ImportError("cannot import name 'ForecastNotAvailable' …")
# and the worker dies at load (diagnosed 2026-06-09). The API service
# imports in a different order, which masked it. Only load_forecast_for_race
# is used at runtime here; ForecastNotAvailable just propagates from it.

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Versioning


# Bumped whenever a RouteRequest field is added, a DeratingProfile field
# is added, or engine behaviour changes in a way that invalidates cached
# results. Part of the route cache key — old entries become unreachable
# (and TTL out) the moment this changes.
ENGINE_VERSION: str = "v13-maneuver"


# Defaults that fall through when the worker has no stored RouteRequest
# yet (race never opened in the editor since Redis flush / since this
# code shipped). Matches the previous worker's hardcoded values so the
# behaviour change is localised to "users who have computed at least
# once will see consistent alerts" without changing what cold-start
# baselines look like.
DEFAULT_DURATION_HOURS: float = 6.0
DEFAULT_POLAR_MARGIN: float = 0.97
DEFAULT_HS_M: float = 0.0
DEFAULT_DENSITY_FACTOR: float = 1.0
DEFAULT_MAX_TWS_KT: Optional[float] = None


# Forecast-window auto-sizing. The route ETA isn't known until *after* the
# forecast loads (the engine needs the wind to compute it), so we can't use
# the real duration to decide how much forecast to load — chicken/egg. Instead
# we size the load window from rhumb-line course distance and a deliberately
# slow nominal passage speed. Erring long costs only a few extra GFS grid
# loads; erring short truncates the route at the forecast horizon (the 6h-cap
# bug this replaces). NOT a route ETA — purely a load-window estimate.
NOMINAL_PASSAGE_SPEED_KT: float = 5.0       # conservative avg SOG for sizing
FORECAST_WINDOW_MARGIN_FRAC: float = 0.15   # proportional cushion on estimate
FORECAST_WINDOW_MARGIN_MIN_H: float = 1.0   # floor on the cushion
GFS_MAX_HORIZON_HOURS: float = 120.0        # hard ceiling — GFS forecast limit
METRES_PER_NM: float = 1852.0


# Engine simulated-time budget (v12). The engine's max_iterations default
# (240 × 5 min = 20h per leg) silently truncated any course longer than 20
# hours of sailing — the route just stopped mid-lake with reached=False.
# We now size the budget from the course itself: double the conservative
# course estimate (slow boat, adverse wind still finishes), with an
# absolute ceiling so a degenerate marks list can't spin the engine for
# days of simulated time. The budget is a TOTAL across legs (the multileg
# driver threads the remainder through), and is independent of the
# forecast window — persist-last-frame wind covers the gap when the
# course outruns the forecast.
ROUTE_DT_MINUTES: float = 5.0               # engine time step; iterations = minutes/dt
SIM_BUDGET_FACTOR: float = 2.0              # budget = factor × course estimate
SIM_BUDGET_MAX_HOURS: float = 240.0         # absolute cap on simulated race time


# ---------------------------------------------------------------------------
# Value types


@dataclass(frozen=True)
class DeratingProfile:
    """User-tunable physical knobs. Embedded in :class:`RouteRequest` so
    a single field change automatically participates in the cache key
    and in the engine-version pinning test.
    """
    max_tws_kt: Optional[float] = DEFAULT_MAX_TWS_KT
    polar_margin: float = DEFAULT_POLAR_MARGIN
    hs_m: float = DEFAULT_HS_M
    density_factor: float = DEFAULT_DENSITY_FACTOR


@dataclass(frozen=True)
class RouteRequest:
    """Fully-resolved input to :func:`compute_route`.

    Both race-bound fields (filled from the DB by the caller) and
    user-tunable knobs (filled from the HTTP body for the endpoint, or
    from the last-stored knobs for the worker) live here. Frozen so the
    pipeline can't mutate it and tests can compare instances by value.
    """
    race_id: UUID
    marks: list[dict]
    race_start: datetime
    boat_class: str            # post-tier resolution (router downgrades free → GENERIC)
    safety_factor: float = DEFAULT_SAFETY_FACTOR
    duration_hours: float = DEFAULT_DURATION_HOURS
    derating: DeratingProfile = field(default_factory=DeratingProfile)


@dataclass(frozen=True)
class RouteOutcome:
    """Pipeline output. The caller chooses how to surface it.

    ``feature`` is a GeoJSON Feature ready to ship over the wire.
    ``meta`` is the dict the API contract documents. ``cached`` lets the
    caller distinguish a fresh compute from a cache hit without
    re-reading the meta dict's ``cached`` field. ``cache_key`` is
    exposed for logging / debugging — callers don't have to use it.
    """
    feature: dict
    meta: dict
    cached: bool
    cache_key: str


# ---------------------------------------------------------------------------
# Region resolution


def resolve_region(marks: list[dict]) -> tuple[str, Optional[str]]:
    """Return ``(base_region, venue_or_None)`` for the marks centroid.

    Base region drives wind + bathymetry lookup (always set; defaults
    to ``'conus'`` for points outside any known base). Venue is set
    only when the centroid falls inside one of the high-res venue
    bboxes — that's the trigger for loading harbour-scale ENC hazards
    alongside the base ones.

    Empty marks list returns ``('conus', None)`` for backwards-compat
    with the previous router helper; callers should already reject
    empty marks before reaching the pipeline.
    """
    if not marks:
        return "conus", None
    lat_c = sum(m["lat"] for m in marks) / len(marks)
    lon_c = sum(m["lon"] for m in marks) / len(marks)
    base = base_region_for_point(lat_c, lon_c)
    venue = venue_for_point(lat_c, lon_c)
    return (
        base.name if base is not None else "conus",
        venue.name if venue is not None else None,
    )


# ---------------------------------------------------------------------------
# Currents loading — same swallow-and-return-None policy as before


async def load_currents_optional(
    marks: list[dict],
    race_start: datetime,
    duration_hours: float,
    race_id: UUID,
) -> Optional[CurrentForecast]:
    """Load a :class:`CurrentForecast` for the race, or return ``None``.

    Currents are non-fatal: any failure (no OFS source covers the marks
    bbox, no ingested cycle, transient Redis blip) returns ``None`` and
    the route still computes. Identical policy to the previous router
    helper so cache behaviour is preserved across the refactor.
    """
    sources = sources_covering_marks(marks)
    if not sources:
        return None
    try:
        return await load_currents_for_race(
            sources=sources,
            race_start=race_start,
            duration_hours=duration_hours,
        )
    except CurrentsUnavailable as exc:
        log.info(
            "currents unavailable for race=%s — proceeding without: %s",
            race_id, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "currents load raised for race=%s (proceeding without currents): %s",
            race_id, exc,
        )
        return None


def _currents_cache_tag(currents: Optional[CurrentForecast]) -> str:
    """Stable string capturing the currents state for cache-key purposes.

    Different cycles of the same source produce different tags so a
    fresh ingest invalidates cached routes. The absence of currents is
    a distinct state from any present state.
    """
    if currents is None:
        return "none"
    return f"{currents.quality}:{currents.t_min.isoformat()}:{currents.t_max.isoformat()}"


def _derating_tag(derating: DeratingProfile) -> str:
    """The derating slice of the cache key. Pulled out as a helper so
    the cache-key builder doesn't sprout an extra argument per knob and
    so a knob added to :class:`DeratingProfile` only needs this string
    extended (and an ``ENGINE_VERSION`` bump)."""
    cutoff = derating.max_tws_kt if derating.max_tws_kt is not None else "-"
    return (
        f"hs={derating.hs_m:.2f}:dens={derating.density_factor:.3f}:"
        f"margin={derating.polar_margin:.3f}:cutoff={cutoff}"
    )


# ---------------------------------------------------------------------------
# Start-wind sampling


def _sample_start_wind(
    forecast,
    start_lat: float,
    start_lon: float,
    race_start: datetime,
) -> tuple[Optional[float], Optional[float]]:
    """Wind at the start mark at race_start, as ``(dir_deg, speed_kt)``.

    Direction uses the meteorological "wind from" convention to match
    ``windBarb.js`` on the frontend. Returns ``(None, None)`` when the
    sample falls outside forecast coverage — rare; would only happen
    for races scheduled past the loaded duration_hours window.
    """
    uv = forecast.sample(start_lat, start_lon, race_start)
    if uv is None:
        return None, None
    u, v = uv
    speed_ms = math.hypot(u, v)
    speed_kt = speed_ms * 1.94384
    dir_deg = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
    return dir_deg, speed_kt


# ---------------------------------------------------------------------------
# Forecast-window sizing


def estimate_course_hours(marks: list[dict]) -> float:
    """Conservative course-time estimate in hours, UNclamped.

    Sums the rhumb-line legs between consecutive marks and divides by a
    conservative nominal passage speed, then adds a margin. NOT a route
    ETA — a deliberately slow sizing estimate used for the forecast load
    window, the engine's simulated-time budget, and the recompute
    worker's "is this race plausibly still running" check.
    """
    if len(marks) < 2:
        return 0.0
    total_m = 0.0
    for a, b in zip(marks, marks[1:]):
        total_m += haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    est_h = (total_m / METRES_PER_NM) / NOMINAL_PASSAGE_SPEED_KT
    est_h += max(FORECAST_WINDOW_MARGIN_MIN_H, FORECAST_WINDOW_MARGIN_FRAC * est_h)
    return est_h


def estimate_load_window_hours(marks: list[dict], floor_hours: float) -> float:
    """Hours of forecast to load so the window spans the whole course.

    The unclamped :func:`estimate_course_hours` clamped to
    ``[floor_hours, GFS_MAX_HORIZON_HOURS]``:

    * never below ``floor_hours`` — the caller's explicit (or default 6h)
      request is a floor, so a buoy race still loads its usual short window;
    * never above the GFS horizon — beyond 120h there is simply no forecast
      to load, so a longer estimate would only mislead. The route no longer
      truncates there either: persist-last-frame sampling carries the
      engine past the last snapshot (v12).

    A long course (e.g. a ~290 nm Mac) lands around 60-70h here, replacing
    the old fixed 6h window that truncated any race over ~6h. Erring long is
    the safe direction: a few extra GFS grids vs. a route that stops mid-lake.
    """
    if len(marks) < 2:
        return floor_hours
    est_h = estimate_course_hours(marks)
    return max(floor_hours, min(est_h, GFS_MAX_HORIZON_HOURS))


def simulated_time_budget_iterations(
    marks: list[dict],
    effective_duration_hours: float,
    dt_minutes: float = ROUTE_DT_MINUTES,
) -> int:
    """Total engine iteration budget for a course (v12).

    ``SIM_BUDGET_FACTOR ×`` the larger of the course estimate and the
    forecast window, capped at :data:`SIM_BUDGET_MAX_HOURS`, converted
    to iterations at ``dt_minutes`` per step. The 2× factor gives slow
    passages (light air, adverse current) room to finish; the cap keeps
    a degenerate marks list from spinning the engine indefinitely.
    """
    budget_hours = min(
        SIM_BUDGET_FACTOR * max(estimate_course_hours(marks), effective_duration_hours),
        SIM_BUDGET_MAX_HOURS,
    )
    return max(1, math.ceil(budget_hours * 60.0 / dt_minutes))


# ---------------------------------------------------------------------------
# The pipeline


async def compute_route(
    req: RouteRequest,
    *,
    redis,
    use_cache: bool,
) -> RouteOutcome:
    """End-to-end route computation.

    Both callers (the router and the recompute worker) hit this. The
    only branch on caller intent is ``use_cache`` — the router wants
    cache reads + writes (cheap repeats of the same race), the worker
    always wants a fresh number to compare against its baseline.

    Raises
    ------
    ForecastNotAvailable
        Race starts past the model horizon. Router maps to HTTP 425
        with ``available_at``; worker silently skips and retries on
        the next ingest cycle.
    RuntimeError
        Operational bug (no ingested cycles, missing snapshot blob).
        Router maps to 503; worker logs and skips.
    BathymetryUnavailable
        Region has no bathymetry ingested yet. Router maps to 503;
        worker logs and skips.
    """
    region, venue = resolve_region(req.marks)
    if region not in REGIONS:
        raise RuntimeError(f"resolved region {region!r} not in registry")

    spec = spec_for_class(req.boat_class)
    polar = load_polar(f"app/services/polars/{spec.polar_csv}")
    min_depth_m = spec.draft_m * req.safety_factor

    # Forecast first — its cycle ids feed the cache key. Imported here,
    # not at module scope, to break the weather↔routing circular import
    # (see the note by the imports at the top of this module).
    from app.services.weather import load_forecast_for_race

    # Size the forecast window to the whole course. req.duration_hours is the
    # floor (explicit caller request / 6h default); a long course bumps it up
    # so the route doesn't truncate at the old fixed-6h horizon.
    effective_duration_hours = estimate_load_window_hours(
        req.marks, req.duration_hours,
    )

    forecast = await load_forecast_for_race(
        region=region,
        race_start=req.race_start,
        duration_hours=effective_duration_hours,
        # v12: routes outrunning the forecast continue on the last
        # frame's wind instead of stopping at the horizon. The tail is
        # flagged via horizon_exceeded in meta and replaced with real
        # data by the recompute worker on the next ingest.
        persist_beyond_horizon=True,
    )

    # Currents — optional. Same race window as the forecast so the
    # cache tag changes when either ingest rotates.
    currents = await load_currents_optional(
        marks=req.marks,
        race_start=req.race_start,
        duration_hours=effective_duration_hours,
        race_id=req.race_id,
    )

    snapshot_sources = "+".join(
        sorted({s.source or "?" for s in forecast.snapshots})
    )
    cache_key = route_cache_key(
        engine_version=ENGINE_VERSION,
        race_id=req.race_id,
        race_start=req.race_start,
        first_snapshot_ref=str(forecast.snapshots[0].reference_time),
        last_snapshot_valid=str(forecast.snapshots[-1].valid_time),
        snapshot_sources=snapshot_sources,
        safety_factor=req.safety_factor,
        venue=venue,
        derating_tag=_derating_tag(req.derating),
        currents_tag=_currents_cache_tag(currents),
    )

    if use_cache:
        cached_blob = await redis.get(cache_key)
        if cached_blob is not None:
            cached = json.loads(cached_blob)
            cached["meta"]["cached"] = True
            log.info("route cache hit race_id=%s", req.race_id)
            return RouteOutcome(
                feature=cached["route"],
                meta=cached["meta"],
                cached=True,
                cache_key=cache_key,
            )

    is_navigable = make_navigable_predicate(
        region=region,
        draft_m=spec.draft_m,
        safety_factor=req.safety_factor,
        venue=venue,
    )

    currents_quality = currents.quality if currents is not None else None

    start_wind_dir_deg, start_wind_speed_kt = _sample_start_wind(
        forecast=forecast,
        start_lat=req.marks[0]["lat"],
        start_lon=req.marks[0]["lon"],
        race_start=req.race_start,
    )

    # v12: size the engine's simulated-time budget to the course instead
    # of accepting the 240-iteration (20h) per-leg default that truncated
    # long races. Total across legs; multileg threads the remainder.
    max_iterations = simulated_time_budget_iterations(
        req.marks, effective_duration_hours,
    )

    log.info(
        "compute route race_id=%s region=%s venue=%s polar=%s race_start=%s "
        "forecast_quality=%s marks=%d max_tws=%s margin=%.3f hs=%.2f dens=%.3f "
        "currents=%s window_h=%.1f budget_iters=%d",
        req.race_id, region, venue, polar.name, req.race_start.isoformat(),
        forecast.quality, len(req.marks), req.derating.max_tws_kt,
        req.derating.polar_margin, req.derating.hs_m, req.derating.density_factor,
        currents_quality or "off", effective_duration_hours, max_iterations,
    )

    result = compute_isochrone_route_multileg(
        marks=req.marks,
        polar=polar,
        wind=forecast,
        is_navigable=is_navigable,
        race_start=req.race_start,
        dt_minutes=ROUTE_DT_MINUTES,
        max_iterations=max_iterations,
        currents=currents,
        max_tws_kt=req.derating.max_tws_kt,
        hs_m=req.derating.hs_m,
        density_factor=req.derating.density_factor,
        polar_margin=req.derating.polar_margin,
    )

    # Persist-last-frame bookkeeping: did the route sail past the last
    # real forecast snapshot? The tail beyond forecast_t_max was computed
    # on steady-state wind and firms up on the next ingest.
    route_end = req.race_start + timedelta(minutes=result.total_minutes)
    if route_end.tzinfo is None:
        route_end = route_end.replace(tzinfo=timezone.utc)
    horizon_exceeded = route_end > forecast.t_max

    feature = route_to_geojson(
        result,
        properties={
            "start": [req.marks[0]["lat"], req.marks[0]["lon"]],
            "finish": [req.marks[-1]["lat"], req.marks[-1]["lon"]],
            "polar": polar.name,
            "boat_class": spec.name,
            "draft_m": spec.draft_m,
            "min_depth_m": min_depth_m,
            "region": region,
            "venue": venue,
            "race_start": req.race_start.isoformat(),
            "forecast_quality": forecast.quality,
            "max_tws_kt": req.derating.max_tws_kt,
            "polar_margin": req.derating.polar_margin,
            "hs_m": req.derating.hs_m,
            "density_factor": req.derating.density_factor,
            "currents_quality": currents_quality,
            "start_wind_dir_deg": start_wind_dir_deg,
            "start_wind_speed_kt": start_wind_speed_kt,
            "forecast_t_max": forecast.t_max.isoformat(),
            "horizon_exceeded": horizon_exceeded,
        },
    )

    meta = {
        "total_minutes": result.total_minutes,
        "tack_count": result.tack_count,
        "reached": result.reached,
        "iterations": result.iterations,
        "nodes_explored": result.nodes_explored,
        "legs": result.legs,
        "region": region,
        "venue": venue,
        "forecast_quality": forecast.quality,
        "race_start": req.race_start.isoformat(),
        "polar": polar.name,
        "boat_class": spec.name,
        "draft_m": spec.draft_m,
        "min_depth_m": min_depth_m,
        "cached": False,
        "max_tws_kt": req.derating.max_tws_kt,
        "polar_margin": req.derating.polar_margin,
        "hs_m": req.derating.hs_m,
        "density_factor": req.derating.density_factor,
        "currents_quality": currents_quality,
        "start_wind_dir_deg": start_wind_dir_deg,
        "start_wind_speed_kt": start_wind_speed_kt,
        "forecast_t_max": forecast.t_max.isoformat(),
        "horizon_exceeded": horizon_exceeded,
    }

    # v12: never cache a truncated route. A reached=False result means
    # the course is blocked or the budget ran out — serving it from
    # cache for the next hour would pin a broken route on the map even
    # after conditions (or code) improve.
    if use_cache and result.reached:
        try:
            await redis.setex(
                cache_key,
                ROUTE_CACHE_TTL_S,
                json.dumps({"route": feature, "meta": meta}),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("route cache write failed: %s", exc)
    elif use_cache:
        log.info(
            "route not cached (reached=False) race_id=%s total_minutes=%.1f",
            req.race_id, result.total_minutes,
        )

    return RouteOutcome(feature=feature, meta=meta, cached=False, cache_key=cache_key)


# ---------------------------------------------------------------------------
# Stored last-request — bridges router → worker for faithful replay


# Subset of the request that the user actually tunes. Race-bound fields
# (marks, start_at, boat_class) come from the DB row at replay time, so
# storing them in Redis would just risk staleness. Storing knobs in
# their own shape (not the full RouteRequest JSON) means a future
# change to RouteRequest's shape doesn't accidentally make old stored
# values unreadable.
@dataclass(frozen=True)
class RouteRequestKnobs:
    safety_factor: float
    duration_hours: float
    derating: DeratingProfile

    @classmethod
    def from_request(cls, req: RouteRequest) -> "RouteRequestKnobs":
        return cls(
            safety_factor=req.safety_factor,
            duration_hours=req.duration_hours,
            derating=req.derating,
        )

    def to_json(self) -> bytes:
        return json.dumps({
            "safety_factor": self.safety_factor,
            "duration_hours": self.duration_hours,
            "derating": asdict(self.derating),
        }).encode()

    @classmethod
    def from_json(cls, blob: bytes) -> "RouteRequestKnobs":
        """Parse stored knobs. Missing fields fall through to dataclass
        defaults — important for forward-compat when a new knob is
        added: stale stored entries keep working, the new knob just
        runs at its default until the user computes once and re-saves.
        """
        data = json.loads(blob)
        d = data.get("derating", {}) or {}
        return cls(
            safety_factor=data.get("safety_factor", DEFAULT_SAFETY_FACTOR),
            duration_hours=data.get("duration_hours", DEFAULT_DURATION_HOURS),
            derating=DeratingProfile(
                max_tws_kt=d.get("max_tws_kt", DEFAULT_MAX_TWS_KT),
                polar_margin=d.get("polar_margin", DEFAULT_POLAR_MARGIN),
                hs_m=d.get("hs_m", DEFAULT_HS_M),
                density_factor=d.get("density_factor", DEFAULT_DENSITY_FACTOR),
            ),
        )


async def save_last_request(req: RouteRequest, *, redis) -> None:
    """Persist the user-tunable knobs from this request under
    ``route:last_request:{race_id}``. The worker reads this on every
    recompute pass so its alert is computed against the same physics
    the user-facing endpoint would produce.

    Cache, not source of truth: 7d TTL, fall-through to defaults when
    missing. Write failures are non-fatal — they only degrade alert
    fidelity, not the user's own compute response.
    """
    blob = RouteRequestKnobs.from_request(req).to_json()
    try:
        await redis.setex(
            route_last_request_key(req.race_id),
            ROUTE_LAST_REQUEST_TTL_S,
            blob,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("last-request save failed race=%s: %s", req.race_id, exc)


async def load_last_request(race_id: UUID, *, redis) -> Optional[RouteRequestKnobs]:
    """Read the stored knobs, or ``None`` if absent / unparseable.

    Absent is the cold-start case (race never opened in the editor
    since deploy / Redis flush). Caller — typically the worker — falls
    back to library defaults and logs the fallback so we can measure
    how often it happens.
    """
    try:
        blob = await redis.get(route_last_request_key(race_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("last-request load failed race=%s: %s", race_id, exc)
        return None
    if blob is None:
        return None
    try:
        return RouteRequestKnobs.from_json(blob)
    except (ValueError, KeyError, TypeError) as exc:
        log.warning(
            "last-request parse failed race=%s (proceeding with defaults): %s",
            race_id, exc,
        )
        return None


def request_with_knobs(
    *,
    race_id: UUID,
    marks: list[dict],
    race_start: datetime,
    boat_class: str,
    knobs: Optional[RouteRequestKnobs],
) -> RouteRequest:
    """Build a :class:`RouteRequest` from canonical race fields plus
    optional stored knobs. Worker entry point.

    When ``knobs`` is ``None`` the request uses library defaults (the
    same set the previous worker hardcoded — :data:`DEFAULT_DURATION_HOURS`,
    :data:`DEFAULT_POLAR_MARGIN`, etc.).
    """
    if knobs is None:
        return RouteRequest(
            race_id=race_id,
            marks=marks,
            race_start=race_start,
            boat_class=boat_class,
        )
    return RouteRequest(
        race_id=race_id,
        marks=marks,
        race_start=race_start,
        boat_class=boat_class,
        safety_factor=knobs.safety_factor,
        duration_hours=knobs.duration_hours,
        derating=knobs.derating,
    )
