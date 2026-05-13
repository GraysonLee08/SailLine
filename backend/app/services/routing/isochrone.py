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

# Used only for the per-point fallback path (legacy callers without
# a `.segment` attribute). The production predicate uses exact line-
# vs-polygon intersection, where this constant doesn't apply.
SEGMENT_FALLBACK_STEP_M = 100.0

# Offset distance applied when seeding the next leg's frontier after a
# rounded mark. 200 m is large enough to comfortably separate the seed
# from the mark (so the next leg's heading sweep doesn't immediately
# trip the rounding-side check) but small enough that the visual bend
# at the mark looks correct on the chart.
ROUNDING_OFFSET_M = 200.0


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


def _segment_check(
    lat1: float, lon1: float, lat2: float, lon2: float,
    is_navigable: Callable[[float, float], bool],
) -> bool:
    """Verify a segment is navigable end-to-end.

    Prefers the exact line-vs-polygon check exposed as
    ``is_navigable.segment(...)`` by ``make_navigable_predicate``.
    Falls back to per-point sampling at SEGMENT_FALLBACK_STEP_M
    intervals for legacy callers (tests with hand-rolled lambdas etc.).
    """
    seg = getattr(is_navigable, "segment", None)
    if seg is not None:
        return seg(lat1, lon1, lat2, lon2)

    # Fallback: per-point sampling.
    distance_m = haversine_m(lat1, lon1, lat2, lon2)
    if distance_m <= 0:
        return is_navigable(lat2, lon2)
    n_checks = max(1, int(math.ceil(distance_m / SEGMENT_FALLBACK_STEP_M)))
    heading = bearing_deg(lat1, lon1, lat2, lon2)
    for i in range(1, n_checks + 1):
        d = distance_m * i / n_checks
        chk_lat, chk_lon = project(lat1, lon1, heading, d)
        if not is_navigable(chk_lat, chk_lon):
            return False
    return True


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
    """
    if is_navigable is None:
        def is_navigable(_lat: float, _lon: float) -> bool:  # type: ignore[misc]
            return True

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
                distance_m = speed_kt * KT_TO_MS * dt_seconds
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
                ))

        if not candidates:
            break

        # Bin by bearing FROM finish; keep the closest-to-finish per bin.
        by_bin: dict[int, tuple[int, float]] = {}  # bin -> (cand_idx, dist_m)
        for cand in candidates:
            d_finish = haversine_m(cand.lat, cand.lon, finish_lat, finish_lon)
            brg = bearing_deg(finish_lat, finish_lon, cand.lat, cand.lon)
            bin_idx = int(brg // bin_width) % angular_bins
            cand_idx_in_list = len(all_nodes)
            all_nodes.append(cand)
            nodes_explored += 1
            current = by_bin.get(bin_idx)
            if current is None or d_finish < current[1]:
                by_bin[bin_idx] = (cand_idx_in_list, d_finish)

        new_frontier = [v[0] for v in by_bin.values()]
        # Check finish hit on the kept set. Require the final approach
        # segment (from the candidate node to the finish mark itself)
        # to be navigable end-to-end — a node within finish_radius is
        # irrelevant if the path between it and the mark crosses land.
        for idx in new_frontier:
            n = all_nodes[idx]
            if haversine_m(n.lat, n.lon, finish_lat, finish_lon) > finish_radius_m:
                continue
            if not _segment_check(
                n.lat, n.lon, finish_lat, finish_lon, is_navigable,
            ):
                continue
            reached_idx = idx
            break
        if reached_idx is not None:
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
    """
    if len(marks) < 2:
        raise ValueError("multi-leg routing needs >= 2 marks")

    legs_completed: list[RouteResult] = []
    elapsed_minutes = 0.0
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

        leg_result = compute_isochrone_route(
            start=current_pos,
            finish=leg_finish,
            polar=polar,
            wind=wind,
            is_navigable=is_navigable,
            race_start=leg_start_dt,
            dt_minutes=dt_minutes,
            heading_step_deg=heading_step_deg,
            max_iterations=max_iterations,
            finish_radius_nm=finish_radius_nm,
            angular_bins=angular_bins,
            currents=currents,
            max_tws_kt=max_tws_kt,
            hs_m=hs_m,
            density_factor=density_factor,
            polar_margin=polar_margin,
            rounding_filter=rf,
        )
        legs_completed.append(leg_result)
        elapsed_minutes += leg_result.total_minutes

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
]
