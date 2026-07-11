# backend/app/services/routing/isochrone.py
"""Pure-numpy isochrone routing engine — time-aware, multi-leg.

Each iteration of dt_minutes:
  - For each frontier point, sweep headings 0..360 by heading_step_deg
  - Sample wind at (lat, lon, valid_time = race_start + iter*dt)
  - Compute TWA, get boat speed from polar (with optional wave / density
    / margin derating), project forward
  - Vector-add surface current (if a currents sampler was supplied)
  - Reject candidates whose segment from parent fails the navigability
    check. The engine prefers an exact ``is_navigable.segment(lat1,
    lon1, lat2, lon2)`` line check when available (catches thin
    obstacles regardless of width) and falls back to per-point
    sampling along the segment when the predicate has no such
    attribute (legacy callers, simple test fixtures).
  - Reject candidates whose TWS exceeds ``max_tws_kt`` (heavy-weather
    cutoff)
  - Cull by bearing-from-finish bins (Hagiwara variant)
  - Stop when within finish_radius_nm AND the final approach segment
    is itself navigable

Time threading: when race_start is provided, the wind argument can be a
WindForecast (multiple snapshots). The engine just calls
wind.sample(lat, lon, valid_time). WindField also accepts the kwarg and
ignores it, so legacy callers and existing tests work unchanged.

Maneuver cost (v13): switching tacks or gybes between a parent node and
its candidate costs time. The candidate's projected distance for the
step is reduced by ``penalty_seconds × boat_speed`` (tack and gybe have
separate penalties). Without this the engine was indifferent between
two long boards and a staircase of one-step boards — modeled time was
identical — and the bearing-bin culling systematically preferred the
staircase because alternating sides hugs the direct bearing to the
finish. The penalty makes consolidated boards strictly faster, so the
optimizer produces sailable routes and prices real maneuver losses
into the ETA. Frontier culling also keeps the top-K nodes per bearing
bin (not just the best) because a parent-heading-dependent penalty
breaks strict isochrone dominance: a slightly-behind node on the
correct tack can beat a slightly-ahead node on the wrong one.

Because a single tack (~46 m at cruising speed) costs far less than
the 5-minute time quantum, penalties alone cannot fully stop weaving:
lineages with a dozen extra tacks still reach on the same iteration.
Three mechanisms close the gap: (1) the culling score charges each
lineage BIN_TIEBREAK_M per cumulative maneuver, (2) when several
nodes reach the finish on one iteration the fewest-maneuvers lineage
wins, and (3) a post-search pass (``_consolidate_boards``) stably
reorders step vectors inside bounded windows to cluster same-tack
steps into boards — time and endpoints unchanged, navigability and
rounding re-checked per window.

Multi-leg: ``compute_isochrone_route_multileg`` accepts a list of marks
and threads elapsed wall-clock across legs so each leg samples the
correct forecast frame. Intermediate marks may carry a ``rounding``
hint ("port" or "starboard"); the engine seeds the next leg's start
position offset to the correct side of the mark by ~200 m. Final mark
is the finish; first mark is the start. No rounding for first/last.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence

import numpy as np


# ─── Constants ──────────────────────────────────────────────────────────

EARTH_RADIUS_M = 6_371_000.0
KT_TO_MS = 0.514_444
M_PER_NM = 1852.0

# (Phase 3 removed the per-point fallback. Predicates always expose
# ``.segment`` — see ``navigability.NavigablePredicate``.)

# Offset distance applied when seeding the next leg's frontier after a
# rounded mark. 200 m is large enough to comfortably separate the seed
# from the mark (so the next leg's heading sweep doesn't immediately
# trip the rounding-side check) but small enough that the visual bend
# at the mark looks correct on the chart.
ROUNDING_OFFSET_M = 200.0

# ── Maneuver penalties (v13) ────────────────────────────────────────────
# Time lost to a tack / gybe, expressed as seconds of sailing removed
# from the step in which the maneuver happens (distance_m is shortened
# by penalty_s × boat_speed). Values are deliberately conservative for
# a ~36 ft keelboat: a clean tack costs ~15 s of VMG, a gybe (kite up)
# a bit more. Both are engine kwargs so callers/tests can tune them;
# setting both to 0.0 restores pre-v13 behaviour exactly.
DEFAULT_TACK_PENALTY_S = 15.0
DEFAULT_GYBE_PENALTY_S = 25.0

# Frontier culling keeps the top-K candidates per bearing bin (v13).
# K=1 was sufficient when the step cost was parent-independent; with
# maneuver penalties, strict per-bin dominance no longer holds — a node
# a boat-length behind but on the correct tack may lead to a faster
# finish. K=2 doubles frontier width, which the v12 budget sizing
# absorbs comfortably.
DEFAULT_FRONTIER_PER_BIN = 2

# Tie-break: when ranking candidates within a bin, each cumulative
# maneuver along a candidate's lineage scores it this many metres
# "further" than a maneuver-free rival. Small relative to a 5-minute
# step (~600–900 m) so it only reorders near-ties; the real
# disincentive is the distance penalty. Cumulative (not per-step)
# because at dt=5min a single tack costs ~46 m — well under the time
# quantum — so per-step nudges alone can't stop weaving lineages.
BIN_TIEBREAK_M = 25.0

# Post-search board consolidation (v13). Within windows of this many
# steps, same-tack steps are clustered by a stable reorder of the
# step displacement vectors — total time and window endpoints are
# unchanged, but the artificial one-step-per-gybe alternation the bin
# culling produces collapses into sailable boards. Windows are only
# accepted when every reordered segment passes navigability (and the
# rounding filter, when present); otherwise the window keeps its
# original order. 24 steps = 2 h at dt=5min, which bounds how far any
# step drifts from the position/time its wind sample came from.
CONSOLIDATION_WINDOW_STEPS = 24


# ─── Geometry primitives (public — tests + scripts import these) ────────


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlon_r = math.radians(lon2 - lon1)
    y = math.sin(dlon_r) * math.cos(lat2_r)
    x = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon_r)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def project(lat: float, lon: float, heading_deg_: float, distance_m: float) -> tuple[float, float]:
    """Great-circle forward projection from (lat, lon) along heading."""
    ang = distance_m / EARTH_RADIUS_M
    h = math.radians(heading_deg_)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(h))
    lon2 = lon1 + math.atan2(
        math.sin(h) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def uv_to_tws_twd(u: float, v: float) -> tuple[float, float]:
    """Convert (u east, v north) m/s to (speed kt, direction-from deg).

    Direction is meteorological 'wind from'. Wind from south = positive v.
    """
    speed_ms = math.hypot(u, v)
    if speed_ms < 1e-6:
        return 0.0, 0.0
    dir_to = (math.degrees(math.atan2(u, v)) + 360.0) % 360.0
    dir_from = (dir_to + 180.0) % 360.0
    return speed_ms / KT_TO_MS, dir_from


def _twa(heading_deg_: float, wind_dir_from_deg: float) -> float:
    diff = (heading_deg_ - wind_dir_from_deg + 360.0) % 360.0
    if diff > 180.0:
        diff = 360.0 - diff
    return diff


def _wind_side(heading_deg_: float, wind_dir_from_deg: float) -> int:
    """Which side the wind is on for a given heading.

    Returns +1 for starboard tack (wind over the starboard side), -1
    for port tack, 0 for dead upwind / dead downwind (no defined side).
    A tack or gybe is a sign flip between parent and candidate.
    """
    rel = (heading_deg_ - wind_dir_from_deg + 360.0) % 360.0
    if rel < 1e-9 or abs(rel - 180.0) < 1e-9:
        return 0
    return 1 if rel > 180.0 else -1


def _consolidate_boards(
    path: list[tuple[float, float]],
    headings: list[float],
    sides: list[int],
    is_navigable,
    window: int,
    rounding_filter: Optional[Callable[[float, float], bool]] = None,
) -> tuple[list[tuple[float, float]], list[float]]:
    """Cluster same-tack steps into boards by reordering step vectors.

    The bin culling induces a tack cadence tied to bin width — even
    with maneuver penalties, the surviving lineage alternates sides
    every few steps because closing speed on the finish *point* is
    maximised on the direct bearing. On a beat/run in near-uniform
    wind, any permutation of the step displacement vectors reaches the
    same endpoint in the same time, so within bounded windows we
    stably reorder steps to put each window's leading side first.
    Stable = intra-side order preserved. Endpoint and total time are
    exactly unchanged; intermediate positions move, which is why the
    window is bounded (limits drift from where each step's wind sample
    was taken) and why every reordered segment is re-checked against
    navigability and the rounding filter — a failing window keeps its
    original order.
    """
    n = len(headings)
    if n < 3 or window < 2 or len(path) < n + 1:
        return path, headings
    new_path: list[tuple[float, float]] = [path[0]]
    new_headings: list[float] = []
    for w0 in range(0, n, window):
        w1 = min(w0 + window, n)
        steps = []
        prev_key = 0
        for i in range(w0, w1):
            dlat = path[i + 1][0] - path[i][0]
            dlon = path[i + 1][1] - path[i][1]
            # Dead up/downwind steps (side 0) travel with the side
            # they follow so they don't split a board.
            key = sides[i] if sides[i] != 0 else prev_key
            prev_key = key
            steps.append((key, dlat, dlon, headings[i]))
        first_key = next((k for k, *_ in steps if k != 0), 0)
        if first_key == 0:
            reordered = steps
        else:
            order = {first_key: 0, 0: 1, -first_key: 1}
            reordered = sorted(steps, key=lambda s: order[s[0]])
        cand_pts = [new_path[-1]]
        for _, dlat, dlon, _ in reordered:
            cand_pts.append((cand_pts[-1][0] + dlat, cand_pts[-1][1] + dlon))
        ok = all(
            is_navigable.segment(cand_pts[j][0], cand_pts[j][1],
                                 cand_pts[j + 1][0], cand_pts[j + 1][1])
            for j in range(len(cand_pts) - 1)
        ) and (
            rounding_filter is None
            or all(rounding_filter(pt[0], pt[1]) for pt in cand_pts[1:])
        )
        chosen = reordered if ok else steps
        if not ok:
            cand_pts = [new_path[-1]]
            for _, dlat, dlon, _ in chosen:
                cand_pts.append((cand_pts[-1][0] + dlat, cand_pts[-1][1] + dlon))
        new_path.extend(cand_pts[1:])
        new_headings.extend(s[3] for s in chosen)
    return new_path, new_headings


def _segment_check(
    lat1: float, lon1: float, lat2: float, lon2: float,
    is_navigable,
) -> bool:
    """Verify a segment is navigable end-to-end via the predicate's
    ``segment`` method.

    Phase 3 removed the per-point fallback. All predicates handed to
    the engine — production or test — must implement
    :class:`~app.services.routing.navigability.NavigablePredicate`.
    Tests that need a trivial predicate use
    :func:`~app.services.routing.navigability.always_navigable` or
    :func:`~app.services.routing.navigability.from_point_func`.
    """
    return is_navigable.segment(lat1, lon1, lat2, lon2)


# ─── Wind field ─────────────────────────────────────────────────────────


@dataclass
class WindField:
    """U/V wind components on a regular lat/lon grid (single snapshot)."""
    lats: np.ndarray
    lons: np.ndarray
    u: np.ndarray
    v: np.ndarray
    reference_time: Optional[str] = None
    valid_time: Optional[str] = None
    source: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "WindField":
        u_key = "u" if "u" in payload else "u10"
        v_key = "v" if "v" in payload else "v10"
        lats = np.asarray(payload["lats"], dtype=np.float64)
        lons = np.asarray(payload["lons"], dtype=np.float64)
        u = np.asarray(payload[u_key], dtype=np.float32)
        v = np.asarray(payload[v_key], dtype=np.float32)
        if lats[0] > lats[-1]:
            lats = lats[::-1]; u = u[::-1, :]; v = v[::-1, :]
        if lons[0] > lons[-1]:
            lons = lons[::-1]; u = u[:, ::-1]; v = v[:, ::-1]
        return cls(
            lats=lats, lons=lons, u=u, v=v,
            reference_time=payload.get("reference_time"),
            valid_time=payload.get("valid_time"),
            source=payload.get("source"),
        )

    def contains(self, lat: float, lon: float) -> bool:
        return (self.lats[0] <= lat <= self.lats[-1]
                and self.lons[0] <= lon <= self.lons[-1])

    def sample(
        self,
        lat: float,
        lon: float,
        valid_time: Optional[datetime] = None,  # accepted for duck-type compat
    ) -> Optional[tuple[float, float]]:
        """Bilinear u, v at (lat, lon). Returns None if out of bounds.

        valid_time is accepted but ignored — a single WindField is one
        snapshot in time. WindForecast.sample is the time-aware variant.
        """
        del valid_time  # explicitly unused
        if not self.contains(lat, lon):
            return None

        i = int(np.searchsorted(self.lats, lat) - 1)
        j = int(np.searchsorted(self.lons, lon) - 1)
        i = max(0, min(i, len(self.lats) - 2))
        j = max(0, min(j, len(self.lons) - 2))

        lat0, lat1 = self.lats[i], self.lats[i + 1]
        lon0, lon1 = self.lons[j], self.lons[j + 1]
        ty = (lat - lat0) / (lat1 - lat0) if lat1 > lat0 else 0.0
        tx = (lon - lon0) / (lon1 - lon0) if lon1 > lon0 else 0.0

        def _bilerp(arr: np.ndarray) -> float:
            a = float(arr[i, j])
            b = float(arr[i, j + 1])
            c = float(arr[i + 1, j])
            d = float(arr[i + 1, j + 1])
            return (a * (1 - tx) * (1 - ty) + b * tx * (1 - ty)
                    + c * (1 - tx) * ty + d * tx * ty)

        return _bilerp(self.u), _bilerp(self.v)


# ─── Engine ─────────────────────────────────────────────────────────────


@dataclass
class _Node:
    lat: float
    lon: float
    heading_deg: float
    parent_idx: Optional[int]
    iteration: int
    maneuvered: bool = False   # this step included a tack/gybe (v13)
    maneuvers: int = 0         # cumulative tacks+gybes along lineage (v13)
    side: int = 0              # wind side of this step's heading (v13)


@dataclass
class RouteResult:
    path: list[tuple[float, float]] = field(default_factory=list)
    headings: list[float] = field(default_factory=list)
    total_minutes: float = 0.0
    tack_count: int = 0
    reached: bool = False
    iterations: int = 0
    nodes_explored: int = 0
    legs: int = 1   # number of legs joined into this path (multi-leg)


def _apply_currents(
    parent_lat: float, parent_lon: float,
    heading: float, distance_m: float,
    currents,
    valid_time: Optional[datetime],
    dt_seconds: float,
) -> tuple[float, float]:
    """Project forward under wind, then offset by current drift over dt.

    ``currents`` is duck-typed: anything with ``.sample(lat, lon, valid_time)``
    returning ``(uc_ms, vc_ms)`` or ``None``. When None or out-of-grid,
    no current is applied — equivalent to the legacy single-vehicle path.
    """
    sailed_lat, sailed_lon = project(parent_lat, parent_lon, heading, distance_m)
    if currents is None:
        return sailed_lat, sailed_lon
    cuv = currents.sample(parent_lat, parent_lon, valid_time)
    if cuv is None:
        return sailed_lat, sailed_lon
    uc, vc = cuv
    drift_m = math.hypot(uc, vc) * dt_seconds
    if drift_m < 1e-3:
        return sailed_lat, sailed_lon
    # u is east, v is north — convert to compass heading where the
    # current is flowing TOWARD.
    drift_heading = (math.degrees(math.atan2(uc, vc)) + 360.0) % 360.0
    return project(sailed_lat, sailed_lon, drift_heading, drift_m)


def compute_isochrone_route(
    start: tuple[float, float],
    finish: tuple[float, float],
    polar,
    wind,                                              # WindField OR WindForecast
    is_navigable: Optional[Callable[[float, float], bool]] = None,
    *,
    race_start: Optional[datetime] = None,
    dt_minutes: float = 5.0,
    heading_step_deg: float = 5.0,
    max_iterations: int = 240,
    finish_radius_nm: float = 0.5,
    angular_bins: int = 72,
    # ── New in v9 ──────────────────────────────────────────────────────
    currents=None,                                     # duck-typed sampler
    max_tws_kt: Optional[float] = None,                # heavy-weather cutoff
    hs_m: float = 0.0,                                 # significant wave height
    density_factor: float = 1.0,                       # ρ/ρ_std
    polar_margin: float = 1.0,                         # gust/perf de-rating
    rounding_filter: Optional[Callable[[float, float], bool]] = None,
    # ── New in v13 ─────────────────────────────────────────────────────
    tack_penalty_s: float = DEFAULT_TACK_PENALTY_S,
    gybe_penalty_s: float = DEFAULT_GYBE_PENALTY_S,
    frontier_per_bin: int = DEFAULT_FRONTIER_PER_BIN,
    # ──────────────────────────────────────────────────────────────────
) -> RouteResult:
    """Find a near-optimal single-leg route from start to finish.

    When ``race_start`` is provided and ``wind`` is a WindForecast, each
    iteration samples wind at race_start + iteration*dt. When
    ``race_start`` is None, behaviour matches the legacy single-snapshot
    engine — useful for tests and the standalone CLI.

    New in v9:
      currents:        optional sampler returning (uc_ms, vc_ms) at
                       (lat, lon, valid_time). Vector-added to projected
                       boat position each iteration.
      max_tws_kt:      heavy-weather cutoff. Candidates from frontier
                       points where TWS exceeds this are not expanded.
      hs_m:            significant wave height for polar derating.
      density_factor:  air density relative to standard (1.225 kg/m³).
      polar_margin:    multiplier in [0, 1] for global polar derating —
                       cheap way to bake in gust/helm-skill margin.
      rounding_filter: optional callable taking (lat, lon) and returning
                       True if a candidate position is on the allowed
                       side of a mark constraint. Used by the multi-leg
                       driver to enforce port/starboard rounding without
                       changing the inner loop.

    New in v13:
      tack_penalty_s:   seconds of sailing lost to a tack. Applied by
                        shortening the maneuvering step's distance.
      gybe_penalty_s:   same, for a gybe (candidate TWA >= 90°).
      frontier_per_bin: candidates kept per bearing bin during culling.
                        Set both penalties to 0 and this to 1 to
                        reproduce pre-v13 behaviour exactly.
    """
    if is_navigable is None:
        # Default to the singleton always-navigable predicate so the
        # engine's `.segment` call works uniformly across paths.
        from app.services.routing.navigability import always_navigable
        is_navigable = always_navigable()

    finish_lat, finish_lon = finish
    start_lat, start_lon = start

    if haversine_m(start_lat, start_lon, finish_lat, finish_lon) / M_PER_NM < finish_radius_nm:
        return RouteResult(path=[start, finish], headings=[0.0],
                           total_minutes=0.0, reached=True,
                           iterations=0, nodes_explored=1)

    dt_seconds = dt_minutes * 60.0
    finish_radius_m = finish_radius_nm * M_PER_NM
    heading_count = int(round(360.0 / heading_step_deg))
    bin_width = 360.0 / angular_bins

    all_nodes: list[_Node] = [
        _Node(lat=start_lat, lon=start_lon, heading_deg=0.0,
              parent_idx=None, iteration=0)
    ]
    frontier: list[int] = [0]
    nodes_explored = 1
    reached_idx: Optional[int] = None
    iteration = 0

    for iteration in range(1, max_iterations + 1):
        # Simulated time at the START of this iteration's expansion. Each
        # parent expands FROM its position AT this moment.
        valid_time = (
            race_start + timedelta(minutes=(iteration - 1) * dt_minutes)
            if race_start is not None else None
        )

        candidates: list[_Node] = []
        for parent_idx in frontier:
            parent = all_nodes[parent_idx]
            uv = wind.sample(parent.lat, parent.lon, valid_time)
            if uv is None:
                # Out of grid OR past forecast horizon. Don't expand.
                continue
            tws_kt, wind_from_deg = uv_to_tws_twd(*uv)
            if tws_kt < 0.5:
                continue
            if max_tws_kt is not None and tws_kt > max_tws_kt:
                # Heavy-weather cutoff: simulate the boat being unable to
                # safely race in this part of the field. The engine
                # naturally routes around the high-wind area as long as
                # alternative paths exist outside the cutoff zone.
                continue

            # Side of the wind the parent was sailing on. The root node
            # (parent_idx None) has no meaningful heading — its
            # expansions are the first real headings, so no penalty.
            parent_side = parent.side if parent.parent_idx is not None else 0

            for k in range(heading_count):
                heading = k * heading_step_deg
                twa = _twa(heading, wind_from_deg)
                speed_kt = polar.boat_speed(
                    twa, tws_kt,
                    hs_m=hs_m,
                    density_factor=density_factor,
                    margin=polar_margin,
                )
                if speed_kt <= 0:
                    continue
                # Maneuver penalty (v13): a sign flip of the wind side
                # between parent and candidate is a tack (candidate TWA
                # < 90°) or gybe (>= 90°). The step still advances
                # dt_seconds of wall clock, but covers less ground.
                cand_side = _wind_side(heading, wind_from_deg)
                maneuvered = False
                effective_dt_s = dt_seconds
                if parent_side != 0 and cand_side != 0 and cand_side != parent_side:
                    maneuvered = True
                    penalty_s = (
                        tack_penalty_s if twa < 90.0 else gybe_penalty_s
                    )
                    effective_dt_s = max(dt_seconds - penalty_s, 0.0)
                distance_m = speed_kt * KT_TO_MS * effective_dt_s
                new_lat, new_lon = _apply_currents(
                    parent.lat, parent.lon, heading, distance_m,
                    currents, valid_time, dt_seconds,
                )
                # Rounding-side filter (multi-leg). Candidates on the
                # wrong side of an enforced rounding constraint are
                # silently rejected.
                if rounding_filter is not None and not rounding_filter(new_lat, new_lon):
                    continue
                # Whole-segment check — exact line-vs-polygon when the
                # predicate exposes .segment, per-point fallback otherwise.
                if not _segment_check(
                    parent.lat, parent.lon, new_lat, new_lon, is_navigable,
                ):
                    continue
                candidates.append(_Node(
                    lat=new_lat, lon=new_lon,
                    heading_deg=heading,
                    parent_idx=parent_idx,
                    iteration=iteration,
                    maneuvered=maneuvered,
                    maneuvers=parent.maneuvers + (1 if maneuvered else 0),
                    side=cand_side,
                ))

        if not candidates:
            break

        # Bin by bearing FROM finish; keep the top-K closest-to-finish
        # per bin (v13 — K > 1 because the maneuver penalty makes step
        # cost parent-dependent, so the single closest node per bin no
        # longer dominates). Ranking charges each lineage a small
        # per-cumulative-maneuver nudge so weaving loses near-ties.
        by_bin: dict[int, list[tuple[float, int]]] = {}  # bin -> [(score, idx)]
        for cand in candidates:
            d_finish = haversine_m(cand.lat, cand.lon, finish_lat, finish_lon)
            brg = bearing_deg(finish_lat, finish_lon, cand.lat, cand.lon)
            bin_idx = int(brg // bin_width) % angular_bins
            cand_idx_in_list = len(all_nodes)
            all_nodes.append(cand)
            nodes_explored += 1
            score = d_finish + cand.maneuvers * BIN_TIEBREAK_M
            by_bin.setdefault(bin_idx, []).append((score, cand_idx_in_list))

        new_frontier: list[int] = []
        for entries in by_bin.values():
            entries.sort(key=lambda t: t[0])
            new_frontier.extend(idx for _, idx in entries[:frontier_per_bin])
        # Check finish hit on the kept set. Require the final approach
        # segment (from the candidate node to the finish mark itself)
        # to be navigable end-to-end — a node within finish_radius is
        # irrelevant if the path between it and the mark crosses land.
        # v13: several lineages typically hit the radius on the same
        # iteration — a tack costs ~46 m, far below the 5-minute time
        # quantum, so weaving and clean lineages "arrive together".
        # Prefer the fewest cumulative maneuvers, then the closest.
        reachers: list[tuple[int, float, int]] = []  # (maneuvers, d_m, idx)
        for idx in new_frontier:
            n = all_nodes[idx]
            d = haversine_m(n.lat, n.lon, finish_lat, finish_lon)
            if d > finish_radius_m:
                continue
            if not _segment_check(
                n.lat, n.lon, finish_lat, finish_lon, is_navigable,
            ):
                continue
            reachers.append((n.maneuvers, d, idx))
        if reachers:
            reachers.sort()
            reached_idx = reachers[0][2]
            break

        frontier = new_frontier

    if reached_idx is None:
        # Closest-approach fallback.
        best_node_idx = min(
            range(len(all_nodes)),
            key=lambda i: haversine_m(all_nodes[i].lat, all_nodes[i].lon,
                                      finish_lat, finish_lon),
        )
        if best_node_idx == 0 and len(all_nodes) == 1:
            return RouteResult(path=[start], headings=[0.0],
                               total_minutes=0.0, reached=False,
                               iterations=iteration, nodes_explored=nodes_explored)
        reached_idx = best_node_idx
        reached = False
    else:
        reached = True

    path_idxs: list[int] = []
    cursor = reached_idx
    while cursor is not None:
        path_idxs.append(cursor)
        cursor = all_nodes[cursor].parent_idx
    path_idxs.reverse()

    path = [(all_nodes[i].lat, all_nodes[i].lon) for i in path_idxs]
    headings = [all_nodes[i].heading_deg for i in path_idxs[1:]]
    sides = [all_nodes[i].side for i in path_idxs[1:]]
    path, headings = _consolidate_boards(
        path, headings, sides, is_navigable, CONSOLIDATION_WINDOW_STEPS,
        rounding_filter=rounding_filter,
    )
    if reached:
        path.append(finish)

    tack_count = 0
    for a, b in zip(headings[:-1], headings[1:]):
        diff = (b - a + 540.0) % 360.0 - 180.0
        if abs(diff) > 60.0:
            tack_count += 1

    total_minutes = (len(path_idxs) - 1) * dt_minutes
    return RouteResult(
        path=path, headings=headings,
        total_minutes=total_minutes, tack_count=tack_count,
        reached=reached, iterations=iteration, nodes_explored=nodes_explored,
        legs=1,
    )


# ─── Multi-leg driver ───────────────────────────────────────────────────


def _rounding_offset_seed(
    mark_lat: float, mark_lon: float,
    next_mark_lat: float, next_mark_lon: float,
    rounding: str,
) -> tuple[float, float]:
    """Offset the next leg's start position to the correct side of the mark.

    "port" rounding = boat keeps mark on its port (left) side. After
    passing the mark heading toward the next mark, the boat is therefore
    to the right of the mark relative to the next-leg bearing. We seed
    the next leg from that offset point.

    "starboard" rounding mirrors this — seed to the left of the next-leg
    bearing.

    The offset is small (200 m) so the geometric "bend" at the mark
    looks correct on the chart. Larger offsets would distort the route;
    smaller offsets risk the next-leg heading sweep tripping the rounding
    side immediately.
    """
    bearing_to_next = bearing_deg(mark_lat, mark_lon, next_mark_lat, next_mark_lon)
    if rounding == "port":
        # Offset to the right of the next-leg bearing.
        offset_heading = (bearing_to_next + 90.0) % 360.0
    elif rounding == "starboard":
        # Offset to the left of the next-leg bearing.
        offset_heading = (bearing_to_next - 90.0 + 360.0) % 360.0
    else:
        return mark_lat, mark_lon
    return project(mark_lat, mark_lon, offset_heading, ROUNDING_OFFSET_M)


def _signed_side(
    point_lat: float, point_lon: float,
    line_lat: float, line_lon: float,
    bearing_to_next_deg: float,
) -> float:
    """Cross-product sign for "is point left or right of a directed line."

    Positive return: point is to the LEFT of the directed line.
    Negative: to the RIGHT.

    Treats the local tangent plane around (line_lat, line_lon) as flat;
    fine for the <50 nm leg scales sailboats care about.
    """
    # Convert bearing to a unit vector in local east-north space.
    h = math.radians(bearing_to_next_deg)
    bx = math.sin(h)   # east component
    by = math.cos(h)   # north component
    # Vector from line point to test point in local east-north metres.
    # Use simple equirectangular approximation; sufficient for <50nm.
    mean_lat = math.radians((point_lat + line_lat) / 2.0)
    dx = math.radians(point_lon - line_lon) * math.cos(mean_lat) * EARTH_RADIUS_M
    dy = math.radians(point_lat - line_lat) * EARTH_RADIUS_M
    # 2D cross product: bx*dy - by*dx > 0 means point is to LEFT of bearing.
    return bx * dy - by * dx


def _make_rounding_filter(
    mark_lat: float, mark_lon: float,
    next_mark_lat: float, next_mark_lon: float,
    rounding: str,
) -> Callable[[float, float], bool]:
    """Build a (lat, lon) -> bool filter that enforces rounding side.

    Filter returns False for candidate positions that lie on the wrong
    side of the line from the mark toward the next mark. Used as the
    next leg's ``rounding_filter`` argument so the boat departs the
    mark on the correct side.
    """
    bearing_to_next = bearing_deg(mark_lat, mark_lon, next_mark_lat, next_mark_lon)

    def _filter(lat: float, lon: float) -> bool:
        # Only enforce within a few miles of the mark — far away, the
        # boat can swing back across the line without breaking the rule.
        if haversine_m(mark_lat, mark_lon, lat, lon) > 3.0 * M_PER_NM:
            return True
        side = _signed_side(lat, lon, mark_lat, mark_lon, bearing_to_next)
        if rounding == "port":
            # Boat keeps mark on its port (left) side. So the boat must
            # be to the RIGHT of the line from mark toward next mark.
            return side < 1.0  # allow tiny tolerance ~exactly on line
        if rounding == "starboard":
            return side > -1.0
        return True

    return _filter


def compute_isochrone_route_multileg(
    marks: Sequence[dict],
    polar,
    wind,
    is_navigable: Optional[Callable[[float, float], bool]] = None,
    *,
    race_start: Optional[datetime] = None,
    dt_minutes: float = 5.0,
    heading_step_deg: float = 5.0,
    max_iterations: int = 240,
    finish_radius_nm: float = 0.5,
    angular_bins: int = 72,
    currents=None,
    max_tws_kt: Optional[float] = None,
    hs_m: float = 0.0,
    density_factor: float = 1.0,
    polar_margin: float = 1.0,
    tack_penalty_s: float = DEFAULT_TACK_PENALTY_S,
    gybe_penalty_s: float = DEFAULT_GYBE_PENALTY_S,
    frontier_per_bin: int = DEFAULT_FRONTIER_PER_BIN,
) -> RouteResult:
    """Route through a multi-mark course, threading wall-clock across legs.

    ``marks`` is a sequence of dicts with at minimum ``lat`` and ``lon``.
    Intermediate marks (not the first, not the last) may carry a
    ``rounding`` key with value "port" or "starboard"; any other value
    or absence means no rounding constraint. The first mark is the start
    (no rounding); the last is the finish (no rounding — you cross it,
    you don't round it).

    Returns a single RouteResult whose ``path`` is the concatenation of
    all legs and whose ``total_minutes`` is the sum. ``legs`` reports the
    number of legs joined. If any leg fails to reach, the result's
    ``reached`` is False and trailing legs are skipped — the partial
    route is still returned for inspection.

    ``max_iterations`` is a TOTAL simulated-time budget shared across
    all legs (v12): each iteration advances dt_minutes of simulated
    time, and every leg receives only what previous legs haven't spent.
    Before v12 each leg got a fresh ``max_iterations``, which still
    capped any single leg at max_iterations*dt of sailing (20h at the
    defaults) — the bug that truncated long point-to-point races
    mid-lake. Callers routing real courses should size the budget from
    the forecast window / course estimate (see
    ``pipeline.compute_route``); the default 240 remains for tests and
    the CLI.
    """
    if len(marks) < 2:
        raise ValueError("multi-leg routing needs >= 2 marks")

    legs_completed: list[RouteResult] = []
    elapsed_minutes = 0.0
    iterations_used = 0
    current_pos = (float(marks[0]["lat"]), float(marks[0]["lon"]))

    for i in range(len(marks) - 1):
        leg_finish = (float(marks[i + 1]["lat"]), float(marks[i + 1]["lon"]))
        leg_start_dt = (
            race_start + timedelta(minutes=elapsed_minutes)
            if race_start is not None else None
        )

        # Rounding filter for THIS leg: only applies if the leg STARTS
        # from an intermediate mark (i > 0). The filter enforces that
        # candidates near the just-rounded mark stay on the correct side.
        rf: Optional[Callable[[float, float], bool]] = None
        if i > 0:
            prev_mark = marks[i]
            prev_rounding = prev_mark.get("rounding")
            if prev_rounding in ("port", "starboard"):
                rf = _make_rounding_filter(
                    mark_lat=float(prev_mark["lat"]),
                    mark_lon=float(prev_mark["lon"]),
                    next_mark_lat=leg_finish[0],
                    next_mark_lon=leg_finish[1],
                    rounding=prev_rounding,
                )

        # Remaining simulated-time budget for this leg (total minus what
        # earlier legs consumed). Exhausted budget = unreachable course
        # within the allotted window; return the partial for inspection.
        iterations_left = max_iterations - iterations_used
        if iterations_left <= 0:
            break

        leg_result = compute_isochrone_route(
            start=current_pos,
            finish=leg_finish,
            polar=polar,
            wind=wind,
            is_navigable=is_navigable,
            race_start=leg_start_dt,
            dt_minutes=dt_minutes,
            heading_step_deg=heading_step_deg,
            max_iterations=iterations_left,
            finish_radius_nm=finish_radius_nm,
            angular_bins=angular_bins,
            currents=currents,
            max_tws_kt=max_tws_kt,
            hs_m=hs_m,
            density_factor=density_factor,
            polar_margin=polar_margin,
            rounding_filter=rf,
            tack_penalty_s=tack_penalty_s,
            gybe_penalty_s=gybe_penalty_s,
            frontier_per_bin=frontier_per_bin,
        )
        legs_completed.append(leg_result)
        elapsed_minutes += leg_result.total_minutes
        iterations_used += leg_result.iterations

        if not leg_result.reached:
            # No point routing onward — return the partial.
            break

        # Seed the next leg. Apply rounding offset if this mark
        # (intermediate, not the finish) has a rounding rule.
        is_intermediate = (i + 1) < (len(marks) - 1)
        if is_intermediate:
            this_mark = marks[i + 1]
            rounding = this_mark.get("rounding")
            if rounding in ("port", "starboard"):
                next_mark = marks[i + 2]
                current_pos = _rounding_offset_seed(
                    mark_lat=leg_finish[0],
                    mark_lon=leg_finish[1],
                    next_mark_lat=float(next_mark["lat"]),
                    next_mark_lon=float(next_mark["lon"]),
                    rounding=rounding,
                )
            else:
                current_pos = leg_finish
        else:
            current_pos = leg_finish

    # Aggregate. Drop duplicate join points between adjacent legs.
    combined_path: list[tuple[float, float]] = []
    combined_headings: list[float] = []
    for i, lr in enumerate(legs_completed):
        if i == 0:
            combined_path.extend(lr.path)
        else:
            # First point of this leg may duplicate the last point of
            # the previous leg (mark coordinate) — skip it.
            combined_path.extend(lr.path[1:] if lr.path else [])
        combined_headings.extend(lr.headings)

    total_minutes = sum(lr.total_minutes for lr in legs_completed)
    tack_count = sum(lr.tack_count for lr in legs_completed)
    iterations = sum(lr.iterations for lr in legs_completed)
    nodes_explored = sum(lr.nodes_explored for lr in legs_completed)
    reached = (
        len(legs_completed) == len(marks) - 1
        and all(lr.reached for lr in legs_completed)
    )

    return RouteResult(
        path=combined_path,
        headings=combined_headings,
        total_minutes=total_minutes,
        tack_count=tack_count,
        reached=reached,
        iterations=iterations,
        nodes_explored=nodes_explored,
        legs=len(legs_completed),
    )


# ─── GeoJSON output ─────────────────────────────────────────────────────


def route_to_geojson(result: RouteResult, properties: Optional[dict] = None) -> dict:
    coords = [[lon, lat] for lat, lon in result.path]
    props: dict = {
        "total_minutes": result.total_minutes,
        "tack_count": result.tack_count,
        "reached": result.reached,
        "iterations": result.iterations,
        "nodes_explored": result.nodes_explored,
        "legs": result.legs,
    }
    if properties:
        props.update(properties)
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }


__all__ = [
    "WindField", "RouteResult",
    "compute_isochrone_route",
    "compute_isochrone_route_multileg",
    "route_to_geojson",
    "haversine_m", "bearing_deg", "project", "uv_to_tws_twd",
    "M_PER_NM", "KT_TO_MS", "EARTH_RADIUS_M",
    "DEFAULT_TACK_PENALTY_S", "DEFAULT_GYBE_PENALTY_S",
    "DEFAULT_FRONTIER_PER_BIN",
]
