"""Section 1.1 — start analysis.

Models the start exactly like the v4 gate detector (``mark_gates``):
a LINE through mark 0 along ``line_bearing_deg`` extending
LINE_HALF_LEN_M to each side, direction-gated toward mark 1.

Line-bearing resolution order (mirrors the detector):
  1. ``race_sessions.start_line_bearing_override`` (user-entered)
  2. ``race_sessions.start_line_bearing_deg``      (system-resolved)
  3. derived here: forecast TWD at the start mark at gun time, +90°

Favored-end convention
----------------------
The course only stores ONE physical start mark, so "pin" vs "boat"
end is not knowable from data. We define **end A** = the end reached
by travelling ``line_bearing_deg`` from the mark, **end B** = the
reciprocal. ``bias_deg = angle_diff(TWD, line_bearing) − 90`` per the
spec; positive bias → end A favored. ``end_started`` reports which
half of the line the boat actually crossed (signed offset along the
bearing axis from the mark). The prompt is told about this convention
so the model never claims to know which physical end was the pin.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

from app.services.race_analysis.geo import (
    angle_diff,
    destination,
    point_segment_distance_m,
    segment_crossing_fraction,
    to_xy,
)
from app.services.race_analysis.preprocess import AnalysisPoint, WindAt, nearest_point

# Same half-length as the v4 detector.
LINE_HALF_LEN_M = 1852.0

# Only look for the first line crossing within this window around the
# gun — a distance race crossing the same water hours later must not
# be read as "finally started".
_CROSS_SEARCH_S = 30 * 60


def resolve_line_bearing(
    *,
    override: Optional[float],
    resolved: Optional[float],
    marks: list[dict],
    gun: Optional[datetime],
    wind_at: Optional[WindAt],
) -> Optional[float]:
    """Resolution order: override → cached system value → TWD+90."""
    if isinstance(override, (int, float)):
        return float(override) % 360.0
    if isinstance(resolved, (int, float)):
        return float(resolved) % 360.0
    if wind_at is None or gun is None or not marks:
        return None
    try:
        lat, lon = float(marks[0]["lat"]), float(marks[0]["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    w = wind_at(lat, lon, gun)
    if w is None:
        return None
    twd, _ = w
    return (twd + 90.0) % 360.0


def line_endpoints(
    mark: dict, line_bearing_deg: float,
) -> Optional[tuple[tuple[float, float], tuple[float, float]]]:
    """(end_a, end_b) lat/lon — end_a at ``line_bearing_deg`` from the
    mark, matching ``mark_gates.build_gates``."""
    try:
        lat, lon = float(mark["lat"]), float(mark["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    a = destination(lat, lon, line_bearing_deg % 360.0, LINE_HALF_LEN_M)
    b = destination(lat, lon, (line_bearing_deg + 180.0) % 360.0, LINE_HALF_LEN_M)
    return a, b


def analyze_start(
    points: list[AnalysisPoint],
    *,
    marks: list[dict],
    gun: Optional[datetime],
    line_bearing_deg: Optional[float],
    wind_at: Optional[WindAt],
) -> Optional[dict]:
    """Compute the 1.1 start block. Returns None when there's no gun
    time, no start mark, or no usable line bearing — the payload just
    omits the section and the prompt degrades gracefully.
    """
    if gun is None or not marks or not points:
        return None
    if line_bearing_deg is None:
        return None
    ends = line_endpoints(marks[0], line_bearing_deg)
    if ends is None:
        return None
    end_a, end_b = ends
    lat0, lon0 = float(marks[0]["lat"]), float(marks[0]["lon"])
    a_xy = to_xy(*end_a, lat0, lon0)
    b_xy = to_xy(*end_b, lat0, lon0)

    # Course side = the side of the line where mark 1 sits. Without a
    # second mark we can't sign OCS; distance still reports.
    ref_side: Optional[float] = None
    if len(marks) > 1:
        try:
            r_xy = to_xy(float(marks[1]["lat"]), float(marks[1]["lon"]), lat0, lon0)
            ref_side = _side(a_xy, b_xy, r_xy)
        except (KeyError, TypeError, ValueError):
            ref_side = None

    at_gun = nearest_point(points, gun)
    if at_gun is None or abs((at_gun.t - gun).total_seconds()) > 60:
        # Recorder wasn't running around the gun — nothing honest to say.
        return None

    p_xy = to_xy(at_gun.lat, at_gun.lon, lat0, lon0)
    dist_m = point_segment_distance_m(p_xy, a_xy, b_xy)
    boat_side = _side(a_xy, b_xy, p_xy)
    ocs: Optional[bool] = None
    if ref_side is not None and ref_side != 0:
        ocs = (boat_side * ref_side) > 0  # on course side at the gun

    # First crossing of the line segment after (gun − 2 min), toward
    # the course side when we know it.
    time_to_cross_s = _first_crossing_s(
        points, gun, a_xy, b_xy, lat0, lon0, ref_side,
    )

    # Wind + bias at the gun.
    bias_deg: Optional[float] = None
    twd_at_gun: Optional[float] = None
    if wind_at is not None:
        w = wind_at(lat0, lon0, gun)
        if w is not None:
            twd_at_gun = w[0]
            bias_deg = _fold90(angle_diff(twd_at_gun, line_bearing_deg) - 90.0)

    # Signed position along the line axis: positive = toward end A.
    axis = math.radians(line_bearing_deg)
    along_m = p_xy[0] * math.sin(axis) + p_xy[1] * math.cos(axis)

    out: dict = {
        "line_bearing_deg": round(line_bearing_deg, 1),
        "distance_to_line_at_gun_m": round(dist_m, 1),
        "sog_at_gun_kts": (
            round(at_gun.sog_kts, 2) if at_gun.sog_kts is not None else None
        ),
        "cog_at_gun_deg": (
            round(at_gun.cog_deg, 1) if at_gun.cog_deg is not None else None
        ),
        "time_to_cross_s": (
            round(time_to_cross_s, 1) if time_to_cross_s is not None else None
        ),
        "ocs": ocs,
        "end_started": "A" if along_m >= 0 else "B",
        "boat_offset_along_line_m": round(along_m, 1),
    }
    if bias_deg is not None:
        out["bias_deg"] = round(bias_deg, 1)
        out["favored_end"] = "A" if bias_deg > 0 else ("B" if bias_deg < 0 else "even")
        out["twd_at_gun_deg"] = round(twd_at_gun, 1)
    return out


def _side(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> float:
    """Sign of p relative to line a→b (2D cross product)."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _fold90(x: float) -> float:
    """Fold a signed angle into (−90, 90] — a line has two reciprocal
    bearings, so bias is only meaningful modulo 180°."""
    while x <= -90.0:
        x += 180.0
    while x > 90.0:
        x -= 180.0
    return x


def _first_crossing_s(
    points: list[AnalysisPoint],
    gun: datetime,
    a_xy: tuple[float, float],
    b_xy: tuple[float, float],
    lat0: float,
    lon0: float,
    ref_side: Optional[float],
) -> Optional[float]:
    """Seconds from gun to the first line crossing (toward the course
    side when known). Searches gun − 2 min .. gun + _CROSS_SEARCH_S so
    a just-early start still registers as a (negative) crossing."""
    t_lo = gun - timedelta(minutes=2)
    t_hi = gun + timedelta(seconds=_CROSS_SEARCH_S)
    prev: Optional[AnalysisPoint] = None
    for p in points:
        if p.t < t_lo:
            prev = p
            continue
        if p.t > t_hi:
            break
        if prev is not None:
            q1 = to_xy(prev.lat, prev.lon, lat0, lon0)
            q2 = to_xy(p.lat, p.lon, lat0, lon0)
            t = segment_crossing_fraction(q1, q2, a_xy, b_xy)
            if t is not None:
                crossed_toward_course = True
                if ref_side is not None and ref_side != 0:
                    s2 = _side(a_xy, b_xy, q2)
                    s1 = _side(a_xy, b_xy, q1)
                    # q2 exactly ON the line (t == 1): direction comes
                    # from where the segment started instead.
                    crossed_toward_course = (
                        s2 * ref_side > 0
                        or (s2 == 0 and s1 * ref_side < 0)
                    )
                if crossed_toward_course:
                    dt = (p.t - prev.t).total_seconds()
                    cross_t = prev.t + timedelta(seconds=dt * t)
                    return (cross_t - gun).total_seconds()
        prev = p
    return None


__all__ = [
    "LINE_HALF_LEN_M", "resolve_line_bearing", "line_endpoints", "analyze_start",
]
