# backend/app/routers/routing.py
"""Route compute endpoint.

POST /api/routing/compute
    Body: {
      "race_id": "<uuid>",
      "safety_factor": 1.5,
      "duration_hours": 6.0,            # optional
      "max_tws_kt": null,               # optional heavy-weather cutoff
      "polar_margin": 0.97,             # optional gust/perf de-rating
      "hs_m": 0.0,                      # optional wave height (until ingest)
      "density_factor": 1.0,            # optional air density factor
    }

Resolves the race, picks the wind region from marks centroid, builds a
time-aware WindForecast spanning the race window, optionally builds a
CurrentForecast from any OFS source whose bbox overlaps the marks bbox,
runs the multi-leg isochrone engine threading simulated time across
every leg, and returns a GeoJSON Feature plus diagnostic metadata.

Region resolution returns a (base_region, venue) pair. base_region drives
wind + bathymetry lookup (always set; defaults to 'conus'). venue is set
only when the marks centroid falls inside a high-res venue bbox — that's
the trigger to load harbour-scale ENC hazards alongside the base ones.

Currents are optional. ``app.currents_regions.sources_covering_marks(...)``
returns 0..N OFS sources for the marks bbox. When 0, the route is
computed with ``currents=None`` and the engine path is unchanged. When
≥1, the currents loader builds a ``CurrentForecast`` that the engine
samples each iteration. If the loader fails for any reason the route
still computes (currents are non-fatal) — the failure is logged and the
meta response reports ``currents_quality: null``.

Forecast not yet available: returns HTTP 425 (Too Early) with
{ available_at, hours_until_available }. The frontend schedules a
refetch at that timestamp.

Cache key: (engine_version, race_id, safety_factor, hrrr_cycle, gfs_cycle,
race_start_iso, snapshot_sources, venue, derating tuple, currents tag).
race_start changes ⇒ cache miss; new cycle ⇒ cache miss; any derating
param change ⇒ cache miss; new currents cycle or transition between
"currents available" and "currents unavailable" ⇒ cache miss.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import db, redis_client
from app.auth import get_current_user
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
from app.services.routing import (
    DEFAULT_SAFETY_FACTOR,
    compute_isochrone_route_multileg,
    make_navigable_predicate,
    route_to_geojson,
)
from app.services.weather import ForecastNotAvailable, load_forecast_for_race

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/routing", tags=["routing"])


# ─── Models ──────────────────────────────────────────────────────────────


class ComputeRouteIn(BaseModel):
    race_id: UUID
    safety_factor: float = Field(default=DEFAULT_SAFETY_FACTOR, ge=1.0, le=3.0)
    duration_hours: float = Field(
        default=6.0, ge=0.5, le=240.0,
        description="How far past race_start to load forecast snapshots. "
                    "Defaults to 6h (covers most inshore/distance races); "
                    "set to ~50 for a Mac.",
    )
    max_tws_kt: Optional[float] = Field(
        default=None, ge=5.0, le=80.0,
        description="Heavy-weather cutoff in knots. Candidates from frontier "
                    "points where forecast TWS exceeds this are not expanded. "
                    "Null = no cutoff.",
    )
    polar_margin: float = Field(
        default=0.97, ge=0.5, le=1.0,
        description="Global multiplier on polar boat speeds. 1.0 = no margin; "
                    "0.97 (default) bakes in a conservative buffer for gust "
                    "variability and helm-skill vs. polar idealization.",
    )
    hs_m: float = Field(
        default=0.0, ge=0.0, le=10.0,
        description="Significant wave height in metres. Until the wave ingest "
                    "worker is online this is caller-supplied; the engine "
                    "applies an upwind penalty / downwind bonus accordingly.",
    )
    density_factor: float = Field(
        default=1.0, ge=0.8, le=1.2,
        description="Air density relative to standard (1.225 kg/m³). Cold "
                    "dense air → >1; hot humid air → <1. Scales effective "
                    "TWS by sqrt(density_factor).",
    )


class RouteMeta(BaseModel):
    total_minutes: float
    tack_count: int
    reached: bool
    iterations: int
    nodes_explored: int
    legs: int
    region: str
    venue: Optional[str] = None
    forecast_quality: str            # "hrrr", "gfs", "hrrr+gfs"
    race_start: Optional[str]
    polar: str
    boat_class: str
    draft_m: float
    min_depth_m: float
    cached: bool
    max_tws_kt: Optional[float] = None
    polar_margin: float = 1.0
    hs_m: float = 0.0
    density_factor: float = 1.0
    # New in v10 — populated when any OFS source covers the marks bbox
    # AND its ingested cycle intersects the race window. None means no
    # currents were folded into the route; the engine ran with
    # ``currents=None``.
    currents_quality: Optional[str] = None
    # New in v10.1 — wind sampled at marks[0] at race_start_at using the
    # same forecast that fed the engine. Lets the frontend's pre-start
    # freshness check (T-5 banner) compute a wind delta against the
    # current forecast without re-running compute. None when the sample
    # falls outside the forecast horizon (rare; would only happen for
    # races scheduled past the loaded duration_hours window).
    start_wind_dir_deg: Optional[float] = None
    start_wind_speed_kt: Optional[float] = None


class ComputeRouteOut(BaseModel):
    route: dict
    meta: RouteMeta


class ForecastPendingOut(BaseModel):
    detail: str
    available_at: str
    hours_until_available: float


# ─── Helpers ─────────────────────────────────────────────────────────────


# Bump on any change to engine inputs/outputs (polar, mask, algorithm).
# v10: surface-currents integration via NOAA OFS ingest.
ENGINE_VERSION = "v10-currents"

ROUTE_CACHE_TTL_S = 3600


def _resolve_region(marks: list[dict]) -> tuple[str, Optional[str]]:
    """Return (base_region, venue_or_None) for the centroid of the marks.

    Base region drives wind + bathymetry lookup (always set, defaults to
    'conus'). Venue is set only when the centroid falls inside one of
    the high-res venue bboxes — that's the trigger for loading
    harbour-scale ENC hazards.
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


async def _load_currents_optional(
    marks: list[dict],
    race_start: datetime,
    duration_hours: float,
    race_id: UUID,
) -> Optional[CurrentForecast]:
    """Load a CurrentForecast for the race, or return None.

    Wraps ``load_currents_for_race`` with the policy that currents are
    optional — a CurrentsUnavailable exception or any other failure
    returns None and the route still computes. The router never fails a
    request because currents weren't available.
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
        # Don't let a currents-side bug take down the whole compute.
        log.warning(
            "currents load raised for race=%s (proceeding without currents): %s",
            race_id, exc,
        )
        return None


def _sample_start_wind(
    forecast,
    start_lat: float,
    start_lon: float,
    race_start: datetime,
) -> tuple[Optional[float], Optional[float]]:
    """Sample wind at the start mark at race_start time.

    Returns ``(dir_deg, speed_kt)`` or ``(None, None)`` when the sample
    falls outside the forecast's spatial or temporal coverage. Used by
    the frontend's T-5 freshness check to detect material forecast
    drift since the route was computed.

    Direction follows the meteorological "wind from" convention to match
    the rest of the app (``uvToSpeedDir`` in ``windBarb.js`` does the
    same: ``atan2(-u, -v)``).
    """
    uv = forecast.sample(start_lat, start_lon, race_start)
    if uv is None:
        return None, None
    u, v = uv
    speed_ms = math.hypot(u, v)
    speed_kt = speed_ms * 1.94384
    dir_deg = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
    return dir_deg, speed_kt


def _currents_cache_tag(currents: Optional[CurrentForecast]) -> str:
    """Stable string capturing the currents state for cache-key purposes.

    Different cycles of the same source produce different tags so a
    fresh ingest invalidates cached routes. The absence of currents is
    a distinct state from "currents present with no coverage" so the
    cache correctly differentiates the two.
    """
    if currents is None:
        return "none"
    return f"{currents.quality}:{currents.t_min.isoformat()}:{currents.t_max.isoformat()}"


async def _assert_race_owned(
    conn: asyncpg.Connection, race_id: UUID, uid: str,
) -> dict:
    row = await conn.fetchrow(
        """
        SELECT id, marks, boat_class, start_at
        FROM race_sessions
        WHERE id = $1 AND user_id = $2
        """,
        race_id, uid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    marks_raw = row["marks"]
    if isinstance(marks_raw, (bytes, str)):
        marks = json.loads(marks_raw)
    else:
        marks = marks_raw or []
    return {
        "id": row["id"],
        "marks": marks,
        "boat_class": row["boat_class"],
        "start_at": row["start_at"],
    }


# ─── Endpoint ────────────────────────────────────────────────────────────


@router.post("/compute", response_model=ComputeRouteOut,
             responses={425: {"model": ForecastPendingOut}})
async def compute_route(
    payload: ComputeRouteIn,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    async with pool.acquire() as conn:
        race = await _assert_race_owned(conn, payload.race_id, user["uid"])

    marks = race["marks"]
    if len(marks) < 2:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "race must have at least 2 marks (start + finish)",
        )

    # Race start: use scheduled start_at; fall back to "now" for races
    # without a gun time set (the user is exploring routing pre-schedule).
    race_start = race["start_at"] or datetime.now(timezone.utc)
    if race_start.tzinfo is None:
        race_start = race_start.replace(tzinfo=timezone.utc)

    region, venue = _resolve_region(marks)
    if region not in REGIONS:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"resolved region {region!r} not in registry",
        )

    log.warning(
        "ROUTING DEBUG region=%s venue=%s has_gfs=%s race_start=%s now+18h=%s",
        region, venue,
        "gfs" in REGIONS[region].sources,
        race_start.isoformat(),
        (datetime.now(timezone.utc) + timedelta(hours=18)).isoformat(),
    )

    spec = spec_for_class(race["boat_class"])
    polar_path = f"app/services/polars/{spec.polar_csv}"
    polar = load_polar(polar_path)
    min_depth_m = spec.draft_m * payload.safety_factor

    # Load the wind forecast first — we need cycle ids for the cache key.
    try:
        forecast = await load_forecast_for_race(
            region=region,
            race_start=race_start,
            duration_hours=payload.duration_hours,
        )
    except ForecastNotAvailable as exc:
        raise HTTPException(
            status_code=425,  # Too Early
            detail={
                "detail": str(exc),
                "available_at": exc.available_at.isoformat(),
                "hours_until_available": exc.hours_until_available,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))

    # Load currents — optional. Same race-window duration as the wind
    # forecast; the loader picks bracketing fhours from each OFS source
    # whose bbox overlaps the marks bbox.
    currents = await _load_currents_optional(
        marks=marks,
        race_start=race_start,
        duration_hours=payload.duration_hours,
        race_id=payload.race_id,
    )

    # Cache key. Cycle iso is stable for a given cycle; race_start changes
    # per scheduled gun time. Forecast quality string captures whether
    # this is HRRR-only, GFS-only, or hybrid. Venue is part of the key
    # so a venue-hazard ingest invalidates cached routes for that venue
    # without touching base-region routes. Derating tuple is included so
    # changing any user-visible polar/cutoff knob is a cache miss.
    # Currents tag invalidates routes when a new OFS cycle lands.
    redis = redis_client.get_client()
    snapshot_sources = "+".join(
        sorted({s.source or "?" for s in forecast.snapshots})
    )
    derating_tag = (
        f"hs={payload.hs_m:.2f}:dens={payload.density_factor:.3f}:"
        f"margin={payload.polar_margin:.3f}:"
        f"cutoff={payload.max_tws_kt if payload.max_tws_kt is not None else '-'}"
    )
    cache_key = (
        f"route:{ENGINE_VERSION}:{payload.race_id}:"
        f"{race_start.isoformat()}:"
        f"{forecast.snapshots[0].reference_time}:{forecast.snapshots[-1].valid_time}:"
        f"{snapshot_sources}:{payload.safety_factor:.2f}:venue={venue or '-'}:"
        f"{derating_tag}:currents={_currents_cache_tag(currents)}"
    )
    cached_blob = await redis.get(cache_key)
    if cached_blob is not None:
        cached = json.loads(cached_blob)
        cached["meta"]["cached"] = True
        log.info("route cache hit race_id=%s", payload.race_id)
        return cached

    try:
        is_navigable = make_navigable_predicate(
            region=region,
            draft_m=spec.draft_m,
            safety_factor=payload.safety_factor,
            venue=venue,
        )
    except BathymetryUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{exc}. Run bathymetry_ingest for this region before computing routes.",
        )

    currents_quality = currents.quality if currents is not None else None

    # Sample the wind the route is "anchored to" at the start mark / gun
    # time. Stamped onto the response meta so the T-5 freshness check
    # can compute a delta against the current forecast without re-doing
    # the route compute.
    start_wind_dir_deg, start_wind_speed_kt = _sample_start_wind(
        forecast=forecast,
        start_lat=marks[0]["lat"],
        start_lon=marks[0]["lon"],
        race_start=race_start,
    )

    log.info(
        "compute route race_id=%s region=%s venue=%s polar=%s race_start=%s "
        "forecast_quality=%s marks=%d max_tws=%s margin=%.3f hs=%.2f dens=%.3f "
        "currents=%s",
        payload.race_id, region, venue, polar.name, race_start.isoformat(),
        forecast.quality, len(marks), payload.max_tws_kt,
        payload.polar_margin, payload.hs_m, payload.density_factor,
        currents_quality or "off",
    )

    result = compute_isochrone_route_multileg(
        marks=marks,
        polar=polar,
        wind=forecast,
        is_navigable=is_navigable,
        race_start=race_start,
        currents=currents,
        max_tws_kt=payload.max_tws_kt,
        hs_m=payload.hs_m,
        density_factor=payload.density_factor,
        polar_margin=payload.polar_margin,
    )

    feature = route_to_geojson(
        result,
        properties={
            "start": [marks[0]["lat"], marks[0]["lon"]],
            "finish": [marks[-1]["lat"], marks[-1]["lon"]],
            "polar": polar.name,
            "boat_class": spec.name,
            "draft_m": spec.draft_m,
            "min_depth_m": min_depth_m,
            "region": region,
            "venue": venue,
            "race_start": race_start.isoformat(),
            "forecast_quality": forecast.quality,
            "max_tws_kt": payload.max_tws_kt,
            "polar_margin": payload.polar_margin,
            "hs_m": payload.hs_m,
            "density_factor": payload.density_factor,
            "currents_quality": currents_quality,
            "start_wind_dir_deg": start_wind_dir_deg,
            "start_wind_speed_kt": start_wind_speed_kt,
        },
    )

    response: dict = {
        "route": feature,
        "meta": {
            "total_minutes": result.total_minutes,
            "tack_count": result.tack_count,
            "reached": result.reached,
            "iterations": result.iterations,
            "nodes_explored": result.nodes_explored,
            "legs": result.legs,
            "region": region,
            "venue": venue,
            "forecast_quality": forecast.quality,
            "race_start": race_start.isoformat(),
            "polar": polar.name,
            "boat_class": spec.name,
            "draft_m": spec.draft_m,
            "min_depth_m": min_depth_m,
            "cached": False,
            "max_tws_kt": payload.max_tws_kt,
            "polar_margin": payload.polar_margin,
            "hs_m": payload.hs_m,
            "density_factor": payload.density_factor,
            "currents_quality": currents_quality,
            "start_wind_dir_deg": start_wind_dir_deg,
            "start_wind_speed_kt": start_wind_speed_kt,
        },
    }

    try:
        await redis.setex(cache_key, ROUTE_CACHE_TTL_S, json.dumps(response))
    except Exception as exc:  # noqa: BLE001
        log.warning("route cache write failed: %s", exc)

    return response
