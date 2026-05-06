# backend/app/services/routing/isochrone.py
"""Pure-numpy isochrone routing engine — time-aware variant.

Each iteration of dt_minutes:
  - For each frontier point, sweep headings 0..360 by heading_step_deg
  - Sample wind at (lat, lon, valid_time = race_start + iter*dt)
  - Compute TWA, get boat speed from polar, project forward
  - Reject candidates that fail the is_navigable predicate
  - Cull by bearing-from-finish bins (Hagiwara variant)
  - Stop when within finish_radius_nm or max_iterations exhausted

Time threading: when race_start is provided, the wind argument can be a
WindForecast (multiple snapshots). The engine just calls
wind.sample(lat, lon, valid_time). WindField also accepts the kwarg and
ignores it, so legacy callers and existing tests work unchanged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

import numpy as np


# ─── Constants ──────────────────────────────────────────────────────────

EARTH_RADIUS_M = 6_371_000.0
KT_TO_MS = 0.514_444
M_PER_NM = 1852.0


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
) -> RouteResult:
    """Find a near-optimal route from start to finish via isochrone fan.

    When `race_start` is provided and `wind` is a WindForecast, each
    iteration samples wind at race_start + iteration*dt. When
    `race_start` is None, behaviour matches the legacy single-snapshot
    engine — useful for tests and the standalone CLI.
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

            for k in range(heading_count):
                heading = k * heading_step_deg
                twa = _twa(heading, wind_from_deg)
                speed_kt = polar.boat_speed(twa, tws_kt)
                if speed_kt <= 0:
                    continue
                distance_m = speed_kt * KT_TO_MS * dt_seconds
                new_lat, new_lon = project(parent.lat, parent.lon, heading, distance_m)
                if not is_navigable(new_lat, new_lon):
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
        # Check finish hit on the kept set.
        for idx in new_frontier:
            n = all_nodes[idx]
            if haversine_m(n.lat, n.lon, finish_lat, finish_lon) <= finish_radius_m:
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
    "compute_isochrone_route", "route_to_geojson",
    "haversine_m", "bearing_deg", "project", "uv_to_tws_twd",
    "M_PER_NM", "KT_TO_MS", "EARTH_RADIUS_M",
]