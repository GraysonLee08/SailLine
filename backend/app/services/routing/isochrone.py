"""Pure-numpy isochrone routing engine.

Time-step fan algorithm:

  1. Start with a single frontier point (the race start).
  2. Each iteration of ``dt_minutes`` minutes:
       - For each frontier point, sweep headings 0..360 in
         ``heading_step_deg`` increments
       - Look up the wind at that point, compute TWA, get boat speed
         from the polar
       - Project the boat forward ``speed × dt`` along the heading
       - Reject candidates that fail the ``is_navigable`` predicate
         (depth + ENC hazards)
  3. **Destination-focused culling**: bin candidates by their bearing
     FROM THE FINISH. Per bin, keep the candidate CLOSEST to finish.
     This preserves spatial diversity around the destination — offshore
     candidates approaching from a different bearing get their own
     bin and survive even when shore-hugging candidates are temporarily
     faster. Compare with the older bearing-from-start / max-distance
     formulation, which is greedy toward "fastest" and gets stuck on
     local obstacles when the optimal-by-VMG path runs into land.
  4. Stop when any frontier point is within ``finish_radius_nm`` of the
     finish, or ``max_iterations`` is reached
  5. Trace back through parent pointers, build the path

The ``is_navigable`` callback is the single integration point with
bathymetry + chart hazards. The engine has no opinion about *why* a
point is unsafe — the predicate decides. Build it via
``app.services.routing.make_navigable_predicate``.

Algorithm references: this is a variant of standard sector-culling
isochrone routing (Hagiwara 1989). Substituting bearing-from-finish for
bearing-from-source as the bin axis is the obstacle-aware variant —
keeps the search admissible to alternate approach paths around
coastline / depth constraints.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np


# ─── Constants ──────────────────────────────────────────────────────────


# Earth radius in meters (mean). Used for great-circle projection.
EARTH_RADIUS_M = 6_371_000.0

# Knots → m/s
KT_TO_MS = 0.514_444

# Meters → nautical miles
M_TO_NM = 1.0 / 1852.0


# ─── Wind field ─────────────────────────────────────────────────────────


@dataclass
class WindField:
    """U/V wind components on a regular lat/lon grid.

    Matches the shape produced by ``backend/workers/weather_ingest.py``
    after JSON serialization. ``u`` is eastward component (m/s),
    ``v`` is northward component (m/s).
    """
    lats: np.ndarray            # 1D ascending, degrees
    lons: np.ndarray            # 1D ascending, degrees
    u: np.ndarray               # 2D shape (len(lats), len(lons)), m/s
    v: np.ndarray               # 2D, m/s
    reference_time: Optional[str] = None
    valid_time: Optional[str] = None
    source: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "WindField":
        """Build a WindField from the JSON payload the weather worker writes.

        Tolerates a couple of key spellings (some downstream code uses
        ``u10``/``v10`` per the GRIB convention; the worker writes the
        shorter ``u``/``v``).
        """
        u_key = "u" if "u" in payload else "u10"
        v_key = "v" if "v" in payload else "v10"

        lats = np.asarray(payload["lats"], dtype=np.float64)
        lons = np.asarray(payload["lons"], dtype=np.float64)
        u = np.asarray(payload[u_key], dtype=np.float32)
        v = np.asarray(payload[v_key], dtype=np.float32)

        # Ensure ascending order — sample() relies on it.
        if lats[0] > lats[-1]:
            lats = lats[::-1]
            u = u[::-1, :]
            v = v[::-1, :]
        if lons[0] > lons[-1]:
            lons = lons[::-1]
            u = u[:, ::-1]
            v = v[:, ::-1]

        return cls(
            lats=lats,
            lons=lons,
            u=u,
            v=v,
            reference_time=payload.get("reference_time"),
            valid_time=payload.get("valid_time"),
            source=payload.get("source"),
        )

    def contains(self, lat: float, lon: float) -> bool:
        return (
            self.lats[0] <= lat <= self.lats[-1]
            and self.lons[0] <= lon <= self.lons[-1]
        )

    def sample(self, lat: float, lon: float) -> tuple[float, float]:
        """Bilinear u, v at (lat, lon). Returns (0, 0) if out of bounds.

        Out-of-bounds returns calm rather than raising — the engine treats
        zero wind as "boat doesn't move," which naturally bounds the
        search to the wind grid extent.
        """
        if not self.contains(lat, lon):
            return 0.0, 0.0

        i = int(np.searchsorted(self.lats, lat, side="right") - 1)
        j = int(np.searchsorted(self.lons, lon, side="right") - 1)
        i = min(max(i, 0), len(self.lats) - 2)
        j = min(max(j, 0), len(self.lons) - 2)

        lat0, lat1 = self.lats[i], self.lats[i + 1]
        lon0, lon1 = self.lons[j], self.lons[j + 1]
        fy = (lat - lat0) / (lat1 - lat0) if lat1 > lat0 else 0.0
        fx = (lon - lon0) / (lon1 - lon0) if lon1 > lon0 else 0.0

        def _bilerp(arr: np.ndarray) -> float:
            v00 = arr[i, j]
            v01 = arr[i, j + 1]
            v10 = arr[i + 1, j]
            v11 = arr[i + 1, j + 1]
            return float(
                (1 - fx) * (1 - fy) * v00
                + fx * (1 - fy) * v01
                + (1 - fx) * fy * v10
                + fx * fy * v11
            )

        return _bilerp(self.u), _bilerp(self.v)


# ─── Geometry helpers ───────────────────────────────────────────────────


def _project(lat: float, lon: float, heading_deg: float, distance_m: float) -> tuple[float, float]:
    """Forward projection along a great circle. Returns (new_lat, new_lon).

    Heading is degrees true; 0 = north, 90 = east.
    """
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    h_r = math.radians(heading_deg)
    d_r = distance_m / EARTH_RADIUS_M

    new_lat_r = math.asin(
        math.sin(lat_r) * math.cos(d_r) + math.cos(lat_r) * math.sin(d_r) * math.cos(h_r)
    )
    new_lon_r = lon_r + math.atan2(
        math.sin(h_r) * math.sin(d_r) * math.cos(lat_r),
        math.cos(d_r) - math.sin(lat_r) * math.sin(new_lat_r),
    )
    return math.degrees(new_lat_r), math.degrees(new_lon_r)


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from (lat1, lon1) to (lat2, lon2), degrees true."""
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlon_r = math.radians(lon2 - lon1)
    y = math.sin(dlon_r) * math.cos(lat2_r)
    x = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon_r)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# ─── Engine ─────────────────────────────────────────────────────────────


@dataclass
class _Node:
    """One position on an isochrone frontier with backpointer to its parent."""
    lat: float
    lon: float
    heading_deg: float       # heading taken to reach this node
    parent_idx: Optional[int]  # index into the iteration's prior frontier
    iteration: int


@dataclass
class RouteResult:
    """Output of a successful (or not) isochrone search."""
    path: list[tuple[float, float]] = field(default_factory=list)
    headings: list[float] = field(default_factory=list)
    total_minutes: float = 0.0
    tack_count: int = 0
    reached: bool = False
    iterations: int = 0
    nodes_explored: int = 0


def _wind_speed_dir(u: float, v: float) -> tuple[float, float]:
    """Convert (u east, v north) m/s to (speed kt, direction-from deg).

    Direction is meteorological "wind from" — i.e. 0° = wind out of the
    north. This is what TWA calculations expect.
    """
    speed_ms = math.hypot(u, v)
    if speed_ms < 1e-6:
        return 0.0, 0.0
    # "From" direction: vector points where wind is going, so flip 180°
    dir_to = (math.degrees(math.atan2(u, v)) + 360.0) % 360.0
    dir_from = (dir_to + 180.0) % 360.0
    return speed_ms / KT_TO_MS, dir_from


def _twa(heading_deg: float, wind_dir_from_deg: float) -> float:
    """True wind angle: 0..180. Symmetric (port/stbd folded)."""
    diff = (heading_deg - wind_dir_from_deg + 360.0) % 360.0
    if diff > 180.0:
        diff = 360.0 - diff
    return diff


def compute_isochrone_route(
    start: tuple[float, float],
    finish: tuple[float, float],
    polar,                                        # app.services.polars.Polar
    wind: WindField,
    is_navigable: Optional[Callable[[float, float], bool]] = None,
    *,
    dt_minutes: float = 5.0,
    heading_step_deg: float = 5.0,
    max_iterations: int = 240,
    finish_radius_nm: float = 0.5,
    angular_bins: int = 72,
) -> RouteResult:
    """Find a near-optimal route from start to finish via isochrone fan.

    Args:
        start: (lat, lon) of the race start
        finish: (lat, lon) of the finish mark
        polar: Polar object with a ``boat_speed(twa, tws_kt)`` method
        wind: WindField with u/v components
        is_navigable: optional (lat, lon) -> bool. False rejects the
            candidate point. None means no land/depth checks (for tests
            and the standalone CLI).
        dt_minutes: time step. 5 min is a reasonable default for inshore
            and short-distance races.
        heading_step_deg: heading sweep granularity. 5° gives 72 candidates
            per node per step.
        max_iterations: hard ceiling. 240 × 5 min = 20 hours.
        finish_radius_nm: how close is "reached." 0.5 nm catches typical
            finish-line widths.
        angular_bins: domination culling bin count. Bins by bearing-from-
            finish; per bin, keeps the candidate closest to the finish.

    Returns:
        RouteResult. ``reached=False`` if the search ran out of iterations
        without getting within finish_radius_nm of the finish.
    """
    if is_navigable is None:
        def is_navigable(_lat: float, _lon: float) -> bool:  # type: ignore[misc]
            return True

    finish_lat, finish_lon = finish
    start_lat, start_lon = start

    # Early out: already inside the finish circle
    if _haversine_m(start_lat, start_lon, finish_lat, finish_lon) * M_TO_NM < finish_radius_nm:
        return RouteResult(
            path=[start, finish],
            headings=[0.0],
            total_minutes=0.0,
            reached=True,
            iterations=0,
            nodes_explored=1,
        )

    dt_seconds = dt_minutes * 60.0
    finish_radius_m = finish_radius_nm / M_TO_NM
    heading_count = int(round(360.0 / heading_step_deg))
    bin_width = 360.0 / angular_bins

    # All explored nodes flat list. parent_idx points into this list.
    all_nodes: list[_Node] = [
        _Node(lat=start_lat, lon=start_lon, heading_deg=0.0,
              parent_idx=None, iteration=0)
    ]
    # Frontier: indices into all_nodes, the latest isochrone.
    frontier: list[int] = [0]
    nodes_explored = 1
    reached_idx: Optional[int] = None

    for iteration in range(1, max_iterations + 1):
        # Generate candidates from each frontier node.
        candidates: list[_Node] = []
        for parent_idx in frontier:
            parent = all_nodes[parent_idx]
            u, v = wind.sample(parent.lat, parent.lon)
            tws_kt, wind_from_deg = _wind_speed_dir(u, v)
            if tws_kt < 0.5:
                # Drift; skip — engine isn't useful below ~1 kt anyway
                continue

            for k in range(heading_count):
                heading = k * heading_step_deg
                twa = _twa(heading, wind_from_deg)
                speed_kt = polar.boat_speed(twa, tws_kt)
                if speed_kt <= 0:
                    continue
                distance_m = speed_kt * KT_TO_MS * dt_seconds
                new_lat, new_lon = _project(parent.lat, parent.lon, heading, distance_m)
                if not is_navigable(new_lat, new_lon):
                    continue
                candidates.append(_Node(
                    lat=new_lat, lon=new_lon,
                    heading_deg=heading,
                    parent_idx=parent_idx,
                    iteration=iteration,
                ))

        if not candidates:
            # Wholly trapped — no frontier extensions survived. Done.
            break

        # Destination-focused culling: bin by bearing FROM the finish,
        # keep the candidate CLOSEST to finish per bin. This preserves
        # spatial diversity around the destination so candidates from
        # different approach bearings (e.g. shore-hugging vs offshore)
        # both survive even when one is temporarily faster — the slower
        # one gets a chance to overtake later when the faster one runs
        # into a wall.
        best_per_bin: dict[int, tuple[float, _Node]] = {}
        for cand in candidates:
            bearing = _bearing_deg(finish_lat, finish_lon, cand.lat, cand.lon)
            bin_idx = int(bearing // bin_width) % angular_bins
            dist_to_finish = _haversine_m(cand.lat, cand.lon, finish_lat, finish_lon)
            cur = best_per_bin.get(bin_idx)
            if cur is None or dist_to_finish < cur[0]:
                best_per_bin[bin_idx] = (dist_to_finish, cand)

        new_frontier_indices: list[int] = []
        for _, cand in best_per_bin.values():
            all_nodes.append(cand)
            idx = len(all_nodes) - 1
            new_frontier_indices.append(idx)
            nodes_explored += 1

            # Check finish reached
            d_to_finish_m = _haversine_m(cand.lat, cand.lon, finish_lat, finish_lon)
            if d_to_finish_m < finish_radius_m:
                if reached_idx is None or all_nodes[reached_idx].iteration > iteration:
                    reached_idx = idx

        frontier = new_frontier_indices

        if reached_idx is not None:
            break

    # If we reached, great. Otherwise return the closest-to-finish node
    # we ever explored (the standard isochrone "closest approach" fallback).
    if reached_idx is None:
        # Search across ALL nodes, not just the final frontier — earlier
        # iterations might have gotten closer to the finish than the
        # current frontier (which has been culled toward "spread around
        # the destination").
        best_node_idx = min(
            range(len(all_nodes)),
            key=lambda i: _haversine_m(
                all_nodes[i].lat, all_nodes[i].lon, finish_lat, finish_lon
            ),
        )
        # If even the start is closer than any explored node (degenerate
        # case where frontier never extended), bail with just [start].
        if best_node_idx == 0 and len(all_nodes) == 1:
            return RouteResult(
                path=[start],
                headings=[0.0],
                total_minutes=0.0,
                reached=False,
                iterations=iteration,
                nodes_explored=nodes_explored,
            )
        reached_idx = best_node_idx
        reached = False
    else:
        reached = True

    # Trace path back to start.
    path_idxs: list[int] = []
    cursor = reached_idx
    while cursor is not None:
        path_idxs.append(cursor)
        cursor = all_nodes[cursor].parent_idx
    path_idxs.reverse()

    path = [(all_nodes[i].lat, all_nodes[i].lon) for i in path_idxs]
    headings = [all_nodes[i].heading_deg for i in path_idxs[1:]]  # skip start (no incoming heading)

    # Append the finish itself as the last point if we reached.
    if reached:
        path.append(finish)

    # Tack count: how many times the heading crosses through the wind
    # axis. Use sign change in TWA at each segment as a proxy for tack
    # vs gybe. Coarse but useful diagnostic.
    tack_count = 0
    for a, b in zip(headings[:-1], headings[1:]):
        # Roll diff into -180..180
        diff = (b - a + 540.0) % 360.0 - 180.0
        if abs(diff) > 60.0:
            tack_count += 1

    total_minutes = (len(path_idxs) - 1) * dt_minutes

    return RouteResult(
        path=path,
        headings=headings,
        total_minutes=total_minutes,
        tack_count=tack_count,
        reached=reached,
        iterations=iteration,
        nodes_explored=nodes_explored,
    )


# ─── GeoJSON output ─────────────────────────────────────────────────────


def route_to_geojson(result: RouteResult, properties: Optional[dict] = None) -> dict:
    """Convert a RouteResult to a GeoJSON Feature (LineString).

    Empty paths produce a Feature with an empty coordinates list — caller
    may want to check ``properties.reached`` before rendering.
    """
    coords = [[lon, lat] for lat, lon in result.path]  # GeoJSON is [lon, lat]
    props: dict = {
        "total_minutes": result.total_minutes,
        "tack_count": result.tack_count,
        "reached": result.reached,
        "iterations": result.iterations,
        "nodes_explored": result.nodes_explored,
    }
    if properties:
        props.update(properties)
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": coords,
        },
        "properties": props,
    }


__all__ = [
    "WindField",
    "RouteResult",
    "compute_isochrone_route",
    "route_to_geojson",
]
