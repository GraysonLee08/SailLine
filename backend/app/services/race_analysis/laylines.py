"""Section 1.5 — laylines and overstands, upwind legs only.

Optimal upwind TWA comes from a VMG sweep over the boat's polar at the
leg-mean TWS (the polar table has no precomputed targets). Overstand is
measured angularly: on the final approach tack, if the bearing to the
mark sits wider off the wind than the optimal TWA, the boat could have
fetched the mark earlier — the lateral excess is

    overstand_m = distance_to_mark × sin(|angle_diff(TWD, brg_to_mark)| − opt_TWA)

which is the perpendicular distance past the layline through the mark.
"Understand" flags extra tacks inside 3 boatlengths of the mark.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from app.services.race_analysis.geo import angle_diff, haversine_m
from app.services.race_analysis.legs import Leg
from app.services.race_analysis.maneuvers import Maneuver

# TWA sweep resolution for the VMG optimum.
_SWEEP_STEP_DEG = 1.0

# Understand: tacks inside this many boatlengths of the mark.
UNDERSTAND_RADIUS_BL = 3.0

# Ignore sub-boatlength "overstands" — GPS noise, not tactics.
_MIN_OVERSTAND_M = 15.0


def optimal_twa(polar, tws_kts: float, *, downwind: bool = False) -> Optional[float]:
    """Best-VMG TWA at ``tws_kts`` from a ``Polar`` (upwind by default).

    Sweeps the table at 1° and maximises |boat_speed × cos(TWA)| over
    the relevant hemisphere. Returns None when the polar gives no
    positive speed anywhere in the range (calm / bad table).
    """
    lo, hi = (91.0, 179.0) if downwind else (1.0, 89.0)
    best_twa: Optional[float] = None
    best_vmg = 0.0
    a = lo
    while a <= hi:
        spd = polar.boat_speed(a, tws_kts)
        vmg = abs(spd * math.cos(math.radians(a)))
        if vmg > best_vmg:
            best_vmg = vmg
            best_twa = a
        a += _SWEEP_STEP_DEG
    return best_twa


def _parse_ts(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def analyze_laylines(
    leg: Leg,
    *,
    polar,
    marks: list[dict],
    maneuvers: list[Maneuver],
    loa_m: float,
) -> Optional[dict]:
    """Layline block for one upwind leg, or None when not computable."""
    if leg.type != "upwind" or leg.mean_tws_kts is None:
        return None
    mark = marks[leg.to_mark_index] if 0 <= leg.to_mark_index < len(marks) else None
    if mark is None:
        return None
    try:
        m_lat, m_lon = float(mark["lat"]), float(mark["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    opt = optimal_twa(polar, leg.mean_tws_kts)
    if opt is None:
        return None

    # Final approach = from the last tack on the leg to the leg end
    # (no tacks → the whole leg is one board).
    leg_tacks = [
        m for m in maneuvers
        if m.kind == "tack" and leg.start_ts <= m.t <= leg.end_ts
    ]
    approach_start = max((m.t for m in leg_tacks), default=leg.start_ts)

    overstand_m = 0.0
    for p in leg.points:
        if p.t < approach_start or p.twd_deg is None:
            continue
        from app.services.race_analysis.geo import bearing_deg as _brg
        brg = _brg(p.lat, p.lon, m_lat, m_lon)
        off_wind = abs(angle_diff(p.twd_deg, brg))
        excess = off_wind - opt
        if excess <= 0:
            continue
        dist = haversine_m(p.lat, p.lon, m_lat, m_lon)
        overstand_m = max(overstand_m, dist * math.sin(math.radians(min(excess, 90.0))))

    if overstand_m < _MIN_OVERSTAND_M:
        overstand_m = 0.0

    understand_tacks = 0
    radius = UNDERSTAND_RADIUS_BL * max(loa_m, 1.0)
    for m in leg_tacks:
        # Distance of the boat to the mark at the tack instant.
        near = min(
            (p for p in leg.points), default=None,
            key=lambda p: abs((p.t - m.t).total_seconds()),
        )
        if near is None:
            continue
        if haversine_m(near.lat, near.lon, m_lat, m_lon) <= radius:
            understand_tacks += 1

    return {
        "optimal_upwind_twa_deg": round(opt, 1),
        "overstand_m": round(overstand_m, 1),
        "understand_tacks_near_mark": understand_tacks,
    }


__all__ = ["optimal_twa", "analyze_laylines", "UNDERSTAND_RADIUS_BL"]
