"""Small planar/spherical geometry helpers shared by the race_analysis
package.

Replicated from ``mark_gates`` / ``race_stats`` rather than imported —
those modules keep their helpers private, and the codebase convention
(see ``performance._twa_deg``) is to replicate small pure helpers so a
leaf package never imports private symbols across module boundaries.

All angles are degrees true unless noted. All distances are metres.
Flat-earth approximations are fine at racecourse scale (≤ a few nm).
"""
from __future__ import annotations

import math
from typing import Optional

EARTH_R_M = 6_371_000.0

KT_TO_MS = 0.514_444
MS_TO_KT = 1.0 / KT_TO_MS
NM_TO_M = 1852.0
FT_TO_M = 0.3048


def angle_diff(a_deg: float, b_deg: float) -> float:
    """Signed smallest angle a − b, normalised to [−180, 180)."""
    return ((a_deg - b_deg + 180.0) % 360.0) - 180.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def destination(
    lat: float, lon: float, bearing: float, dist_m: float,
) -> tuple[float, float]:
    """Flat-earth destination point — fine at gate scales (≤ 2 km)."""
    b = math.radians(bearing)
    dlat = dist_m * math.cos(b) / EARTH_R_M
    dlon = dist_m * math.sin(b) / (EARTH_R_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Equirectangular projection to metres, centred on (lat0, lon0)."""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_R_M
    y = math.radians(lat - lat0) * EARTH_R_M
    return x, y


def point_segment_distance_m(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """Distance from point ``p`` to segment ``a``→``b``, planar (xy in m)."""
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 <= 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def cross2(ox: float, oy: float, ax: float, ay: float, bx: float, by: float) -> float:
    """2D cross product of (a − o) × (b − o)."""
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def segment_crossing_fraction(
    p1: tuple[float, float],
    p2: tuple[float, float],
    g1: tuple[float, float],
    g2: tuple[float, float],
) -> Optional[float]:
    """Fraction t ∈ [0, 1] along p1→p2 where it crosses segment g1→g2,
    or None if the segments don't intersect. Same semantics as
    ``mark_gates._segment_crossing_fraction`` (collinear slide is not a
    crossing)."""
    d1 = cross2(g1[0], g1[1], g2[0], g2[1], p1[0], p1[1])
    d2 = cross2(g1[0], g1[1], g2[0], g2[1], p2[0], p2[1])
    d3 = cross2(p1[0], p1[1], p2[0], p2[1], g1[0], g1[1])
    d4 = cross2(p1[0], p1[1], p2[0], p2[1], g2[0], g2[1])
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        denom = d1 - d2
        if denom == 0:
            return None
        return d1 / denom
    return None


def circular_mean_deg(angles_deg: list[float]) -> Optional[float]:
    """Mean of directional data in degrees, or None for empty input."""
    if not angles_deg:
        return None
    s = sum(math.sin(math.radians(a)) for a in angles_deg)
    c = sum(math.cos(math.radians(a)) for a in angles_deg)
    if abs(s) < 1e-12 and abs(c) < 1e-12:
        return None
    return math.degrees(math.atan2(s, c)) % 360.0


def uv_to_tws_twd(u: float, v: float) -> tuple[float, float]:
    """Convert (u east, v north) m/s to (speed kt, wind-FROM deg true).

    Identical math to ``performance._uv_to_tws_twd`` — the ``atan2(-u, -v)``
    form in the analysis spec is the same expression, folded differently.
    """
    speed_ms = math.hypot(u, v)
    if speed_ms < 1e-6:
        return 0.0, 0.0
    dir_to = (math.degrees(math.atan2(u, v)) + 360.0) % 360.0
    return speed_ms * MS_TO_KT, (dir_to + 180.0) % 360.0


def signed_twa_deg(twd_deg: float, cog_deg: float) -> float:
    """Signed true wind angle in [−180, 180).

    Convention (fixed for the whole race_analysis package):
    ``twa = angle_diff(TWD, COG)`` — **negative = port tack, positive =
    starboard tack**. Example: wind from 000°, boat heading 045° → wind
    over the port bow → −45 → port tack.
    """
    return angle_diff(twd_deg, cog_deg)


__all__ = [
    "EARTH_R_M", "KT_TO_MS", "MS_TO_KT", "NM_TO_M", "FT_TO_M",
    "angle_diff", "haversine_m", "bearing_deg", "destination", "to_xy",
    "point_segment_distance_m", "cross2", "segment_crossing_fraction",
    "circular_mean_deg", "uv_to_tws_twd", "signed_twa_deg",
]
