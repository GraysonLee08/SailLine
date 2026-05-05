"""Isochrone routing engine.

Pure-numpy time-step fan. From each frontier point we sweep headings every
``heading_step_deg``, look up boat speed against the polar at the local
TWA/TWS, project forward ``dt_minutes`` minutes, and accumulate a new
frontier. Dominated points are culled by binning on bearing-from-start and
keeping the candidate closest to finish in each bin.

This is the simplest version of the algorithm that produces sailable
results — no obstacles, no currents, no time-varying wind, single
deterministic forecast. The Saturday May 9 delivery test treats whatever
this produces as the baseline; richer features land in subsequent sprints.

Wind sampling: ``WindField`` wraps the pre-clipped JSON the ingest worker
writes to Redis (lats[], lons[], u[][], v[][] in m/s, meteorological
convention where positive u is eastward and positive v is northward). It
exposes ``sample(lat, lon)`` for bilinear u/v lookup.

Coordinate math: spherical earth at the local latitude. Good to better
than 0.1% over a 50nm passage — well below the wind/polar uncertainty.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.services.polars import Polar


# ─── Constants ──────────────────────────────────────────────────────────

EARTH_RADIUS_M = 6_371_000.0
MS_PER_KT = 0.5144444
M_PER_NM = 1852.0


# ─── Wind field ─────────────────────────────────────────────────────────


@dataclass
class WindField:
    """Bilinear-interpolated u/v wind on a regular lat/lon grid.

    lats: 1D ascending (degrees)
    lons: 1D ascending (degrees)
    u, v: 2D shape (len(lats), len(lons)) in m/s
    """
    lats: np.ndarray
    lons: np.ndarray
    u: np.ndarray
    v: np.ndarray
    reference_time: Optional[str] = None
    valid_time: Optional[str] = None
    source: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "WindField":
        """Construct from the ingest worker's JSON payload shape.

        Tolerates lats either ascending or descending — flips internally so
        bracket math always sees ascending arrays.
        """
        lats = np.asarray(payload["lats"], dtype=np.float64)
        lons = np.asarray(payload["lons"], dtype=np.float64)
        u = np.asarray(payload["u"], dtype=np.float64)
        v = np.asarray(payload["v"], dtype=np.float64)

        if lats[0] > lats[-1]:
            lats = lats[::-1]
            u = u[::-1, :]
            v = v[::-1, :]
        if lons[0] > lons[-1]:
            lons = lons[::-1]
            u = u[:, ::-1]
            v = v[:, ::-1]

        return cls(
            lats=lats, lons=lons, u=u, v=v,
            reference_time=payload.get("reference_time"),
            valid_time=payload.get("valid_time"),
            source=payload.get("source"),
        )

    def contains(self, lat: float, lon: float) -> bool:
        return (
            self.lats[0] <= lat <= self.lats[-1]
            and self.lons[0] <= lon <= self.lons[-1]
        )

    def sample(self, lat: float, lon: float) -> Optional[tuple[float, float]]:
        """Bilinear u/v at (lat, lon). None if outside the grid."""
        if not self.contains(lat, lon):
            return None

        i = int(np.searchsorted(self.lats, lat, side="right") - 1)
        j = int(np.searchsorted(self.lons, lon, side="right") - 1)
        i = min(max(i, 0), len(self.lats) - 2)
        j = min(max(j, 0), len(self.lons) - 2)

        lat0, lat1 = self.lats[i], self.lats[i + 1]
        lon0, lon1 = self.lons[j], self.lons[j + 1]
        fy = (lat - lat0) / (lat1 - lat0) if lat1 > lat0 else 0.0
        fx = (lon - lon0) / (lon1 - lon0) if lon1 > lon0 else 0.0

        u = (
            (1 - fx) * (1 - fy) * self.u[i, j]
            + fx * (1 - fy) * self.u[i, j + 1]
            + (1 - fx) * fy * self.u[i + 1, j]
            + fx * fy * self.u[i + 1, j + 1]
        )
        v = (
            (1 - fx) * (1 - fy) * self.v[i, j]
            + fx * (1 - fy) * self.v[i, j + 1]
            + (1 - fx) * fy * self.v[i + 1, j]
            + fx * fy * self.v[i + 1, j + 1]
        )
        return float(u), float(v)


# ─── Geometry ───────────────────────────────────────────────────────────


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing in degrees (0=N, 90=E) from p1 to p2."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def project(lat: float, lon: float, heading_deg_: float, distance_m: float) -> tuple[float, float]:
    """Project (lat, lon) along a constant bearing for distance_m meters."""
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    h = math.radians(heading_deg_)
    d = distance_m / EARTH_RADIUS_M

    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(h))
    l2 = l1 + math.atan2(
        math.sin(h) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), ((math.degrees(l2) + 540.0) % 360.0) - 180.0


def uv_to_tws_twd(u_ms: float, v_ms: float) -> tuple[float, float]:
    """(u, v) in m/s → (TWS in kts, TWD in deg, wind FROM direction)."""
    tws_ms = math.hypot(u_ms, v_ms)
    twd = (math.degrees(math.atan2(-u_ms, -v_ms)) + 360.0) % 360.0
    return tws_ms / MS_PER_KT, twd


def angular_diff(a: float, b: float) -> float:
    """Smallest angle between two bearings, 0..180."""
    return abs(((a - b + 540.0) % 360.0) - 180.0)


# ─── Engine ─────────────────────────────────────────────────────────────


@dataclass
class IsochroneResult:
    """The raw output of the engine. Convert to GeoJSON via route_to_geojson()."""
    coords: list[tuple[float, float]]   # [(lat, lon), ...] including start and finish
    total_minutes: float
    tack_count: int
    iterations: int
    reached: bool                        # True if we hit the finish radius
    nodes_explored: int


def compute_isochrone_route(
    start: tuple[float, float],
    finish: tuple[float, float],
    polar: Polar,
    wind: WindField,
    *,
    dt_minutes: float = 5.0,
    heading_step_deg: float = 5.0,
    max_iterations: int = 240,
    finish_radius_nm: float = 0.5,
    angular_bins: int = 144,
    tack_threshold_deg: float = 80.0,
) -> IsochroneResult:
    """Run isochrone routing from start to finish.

    Args:
        start, finish: (lat, lon) tuples in degrees.
        polar: class polar (see app.services.polars).
        wind: WindField wrapping a single-time-step wind grid.
        dt_minutes: time step per fan iteration. 5 min is a good default
            for inshore/coastal; 10 min for distance.
        heading_step_deg: heading sweep granularity. 5° = 72 directions.
        max_iterations: hard cap. 240 × 5 min = 20 hours.
        finish_radius_nm: a node within this distance terminates the search.
        angular_bins: resolution of the bearing-from-start culling. 144
            bins = 2.5°/bin — plenty for v0.
        tack_threshold_deg: heading change above this between consecutive
            segments is counted as a tack/gybe.
    """
    finish_lat, finish_lon = finish
    start_lat, start_lon = start
    finish_radius_m = finish_radius_nm * M_PER_NM

    # Flat parallel arrays — faster than list-of-dicts and easier to
    # vectorize later. parent[i] = -1 means root.
    lats = [start_lat]
    lons = [start_lon]
    times = [0.0]
    parents = [-1]
    headings: list[Optional[float]] = [None]

    frontier: list[int] = [0]

    # Early-out: starting already inside the finish radius is degenerate
    # but we should still produce a route (just start→finish).
    if haversine_m(start_lat, start_lon, finish_lat, finish_lon) <= finish_radius_m:
        return IsochroneResult(
            coords=[(start_lat, start_lon), (finish_lat, finish_lon)],
            total_minutes=0.0,
            tack_count=0,
            iterations=0,
            reached=True,
            nodes_explored=1,
        )

    bin_width = 360.0 / angular_bins
    headings_sweep = np.arange(0.0, 360.0, heading_step_deg)

    iterations = 0
    reached_idx: Optional[int] = None

    for it in range(1, max_iterations + 1):
        iterations = it
        # bin_idx -> (best_node_idx, dist_to_finish_m)
        buckets: dict[int, tuple[int, float]] = {}

        for parent_idx in frontier:
            plat = lats[parent_idx]
            plon = lons[parent_idx]
            uv = wind.sample(plat, plon)
            if uv is None:
                continue
            tws_kts, twd_deg = uv_to_tws_twd(uv[0], uv[1])

            for hdg in headings_sweep:
                twa = angular_diff(float(hdg), twd_deg)
                speed_kts = polar.boat_speed(twa, tws_kts)
                if speed_kts <= 0.0:
                    continue
                dist_m = speed_kts * MS_PER_KT * dt_minutes * 60.0
                lat2, lon2 = project(plat, plon, float(hdg), dist_m)

                if not wind.contains(lat2, lon2):
                    continue

                d_finish = haversine_m(lat2, lon2, finish_lat, finish_lon)
                brg = bearing_deg(start_lat, start_lon, lat2, lon2)
                bidx = int(brg / bin_width) % angular_bins

                cur = buckets.get(bidx)
                if cur is not None and d_finish >= cur[1]:
                    continue

                node_idx = len(lats)
                lats.append(lat2)
                lons.append(lon2)
                times.append(times[parent_idx] + dt_minutes)
                parents.append(parent_idx)
                headings.append(float(hdg))
                buckets[bidx] = (node_idx, d_finish)

        if not buckets:
            break  # nowhere to go — wind grid edge or polar refusing all headings

        new_frontier = [v[0] for v in buckets.values()]

        # Termination: any new node within the finish radius wins (fastest).
        finishers = [
            idx for idx in new_frontier
            if haversine_m(lats[idx], lons[idx], finish_lat, finish_lon) <= finish_radius_m
        ]
        if finishers:
            finishers.sort(key=lambda i: times[i])
            reached_idx = finishers[0]
            break

        frontier = new_frontier

    # If we didn't reach the finish, take the best closest-approach node
    # from any iteration (not just the last frontier — the last frontier
    # may be empty if the search stalled at a wind-grid edge).
    if reached_idx is None:
        # Search all nodes for the one closest to finish
        best_idx = 0
        best_d = float("inf")
        for i in range(1, len(lats)):
            d = haversine_m(lats[i], lons[i], finish_lat, finish_lon)
            if d < best_d:
                best_d = d
                best_idx = i
        end_idx = best_idx
        reached = False
    else:
        end_idx = reached_idx
        reached = True

    # Backtrack
    path: list[int] = []
    cur = end_idx
    while cur >= 0:
        path.append(cur)
        cur = parents[cur]
    path.reverse()

    coords: list[tuple[float, float]] = [(lats[i], lons[i]) for i in path]
    if reached:
        # Append the actual finish so the line terminates exactly on the mark
        coords.append((finish_lat, finish_lon))

    # Tack count: heading change between consecutive segments
    tack_count = 0
    for k in range(2, len(path)):
        h1 = headings[path[k - 1]]
        h2 = headings[path[k]]
        if h1 is None or h2 is None:
            continue
        if angular_diff(h1, h2) >= tack_threshold_deg:
            tack_count += 1

    return IsochroneResult(
        coords=coords,
        total_minutes=times[end_idx],
        tack_count=tack_count,
        iterations=iterations,
        reached=reached,
        nodes_explored=len(lats),
    )


# ─── GeoJSON output ─────────────────────────────────────────────────────


def route_to_geojson(result: IsochroneResult, *, properties: Optional[dict] = None) -> dict:
    """Convert an IsochroneResult to a GeoJSON Feature (LineString).

    The frontend loads this into a Mapbox geojson source. Properties carry
    diagnostics so the UI can render a badge ("4h 12m · 2 tacks").
    """
    props = {
        "total_minutes": round(result.total_minutes, 1),
        "tack_count": result.tack_count,
        "reached": result.reached,
        "iterations": result.iterations,
        "nodes_explored": result.nodes_explored,
    }
    if properties:
        props.update(properties)

    return {
        "type": "Feature",
        "properties": props,
        "geometry": {
            "type": "LineString",
            # GeoJSON uses (lon, lat) order
            "coordinates": [[lon, lat] for (lat, lon) in result.coords],
        },
    }
