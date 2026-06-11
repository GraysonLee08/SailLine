"""Call-candidate detectors for the in-race tactician.

Pure functions, no I/O. The pipeline loads context (recent track,
per-fix performance evals, forecast, active route, heel statistic) and
calls ``run_detectors``; each detector returns a ``CallCandidate`` or
``None``. Claude later phrases the winning candidate — detectors own
*whether and when* to speak, never the wording.

Two call classes (spec 2026-06-11, "lead time is the product"):

* ``maneuver`` — predictive; carries an ``eta``. Fires only when the
  projected event enters the announce window
  [ANNOUNCE_MIN_LEAD_S, ANNOUNCE_MAX_LEAD_S]. The pipeline re-checks
  the eta after the Claude round-trip and DROPS the call if it would
  arrive inside the minimum lead — late calls are never delivered.
* ``coaching`` — a condition true *right now* (pinching, over-heel,
  off-pace). No eta; the pipeline's persistence rule applies (the
  condition was detected on the freshest batch, one call + one
  reminder per episode via per-type cooldowns).

Geometry note: all small-scale math uses a local equirectangular
projection (dx scaled by cos(lat)), the same approximation
``routing.isochrone`` uses for its cross-track helper. Fine at
race scale (< a few nm); nobody is calling laylines across an ocean.

Inputs are plain dicts/lists to match the house style
(``performance.py``, ``heel_stats.py``):

* ``track``  — ascending ``{t, lat, lon, sog_kts, cog_deg}`` (~5 min)
* ``evals``  — ascending ``{t, **evaluate_point-output}`` for the same
  fixes (only the evaluable ones)
* ``forecast`` — duck-typed ``.sample(lat, lon, t) -> (u, v) | None``
* ``route_coords`` — active plan LineString as ``[(lon, lat), ...]``
  (GeoJSON axis order), or None when no route is computed
* ``next_mark`` — ``{lat, lon, label?}`` or None
* ``heel_stat`` — output of ``heel.sustained_heel`` or None
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from app.services.tactics.heel_bands import band_for

# ─── Tunables (spec open-question starting values — tune on water) ──────

# Crew-reaction budget. A maneuver call must reach the phone at least
# this long before the event.
ANNOUNCE_MIN_LEAD_S: float = 90.0
# Don't announce events further out than this — too early reads as noise.
ANNOUNCE_MAX_LEAD_S: float = 300.0

# Course change along the planned route that counts as a maneuver.
TURN_THRESHOLD_DEG: float = 25.0
# Boat must be within this of the plan for plan-based detectors.
ON_PLAN_MAX_XTE_M: float = 500.0
# Beyond this cross-track error the divergence detector fires.
DIVERGENCE_XTE_M: float = 300.0

# Forecast-shift detector: direction change at the boat's position
# within the look-ahead horizon that's worth a heads-up.
SHIFT_THRESHOLD_DEG: float = 12.0
SHIFT_HORIZONS_S: tuple[float, ...] = (300.0, 600.0, 900.0)

# Pinching / sailing-low: sustained deviation from the polar's best-VMG
# angle. Asymmetric — sailing low is partially self-announcing (boat
# feels fast), pinching isn't.
PINCH_DEG: float = 7.0
SAIL_LOW_DEG: float = 10.0
ANGLE_SUSTAIN_S: float = 45.0
UPWIND_TWA_MAX_DEG: float = 80.0

# Off-pace: sustained speed-ratio deficit.
OFF_PACE_RATIO: float = 0.88
OFF_PACE_SUSTAIN_S: float = 90.0
# A COG swing this large inside the window means a tack/gybe — suppress
# pace/angle coaching across maneuvers.
MANEUVER_COG_SWING_DEG: float = 40.0

_MIN_SOG_KT: float = 1.5      # below this the boat is drifting; no calls
_MIN_EVAL_SAMPLES: int = 5


# ─── Candidate shape ─────────────────────────────────────────────────────

# Lower number = higher priority when multiple detectors fire at once.
PRIORITY: dict[str, int] = {
    "planned_maneuver": 1,
    "layline": 2,
    "forecast_shift": 3,
    "over_heel": 4,
    "pinching": 5,
    "off_pace": 6,
    "plan_divergence": 7,
}


@dataclass(frozen=True)
class CallCandidate:
    call_type: str
    call_class: str                      # "maneuver" | "coaching"
    diagnosis: dict
    eta: Optional[datetime] = None       # maneuver class only
    adjustments: tuple[str, ...] = field(default_factory=tuple)

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.call_type, 99)


# ─── Geometry helpers ────────────────────────────────────────────────────

_EARTH_R_M = 6_371_000.0
_KT_TO_MS = 0.514_444


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing, degrees true."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _ang_diff(a: float, b: float) -> float:
    """Signed smallest angle a−b in (−180, 180]."""
    return ((a - b + 540.0) % 360.0) - 180.0


def _xte_to_polyline_m(
    lat: float, lon: float, coords: Sequence[tuple[float, float]],
) -> tuple[float, int]:
    """(min cross-track distance, index of nearest segment start).

    ``coords`` in GeoJSON (lon, lat) order. Equirectangular local
    projection per segment — adequate at race scale.
    """
    best = float("inf")
    best_i = 0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        mean_lat = math.radians((lat1 + lat2) / 2.0)
        ax = math.radians(lon1) * math.cos(mean_lat) * _EARTH_R_M
        ay = math.radians(lat1) * _EARTH_R_M
        bx = math.radians(lon2) * math.cos(mean_lat) * _EARTH_R_M
        by = math.radians(lat2) * _EARTH_R_M
        px = math.radians(lon) * math.cos(mean_lat) * _EARTH_R_M
        py = math.radians(lat) * _EARTH_R_M
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 <= 0:
            d = math.hypot(px - ax, py - ay)
        else:
            u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
            d = math.hypot(px - (ax + u * dx), py - (ay + u * dy))
        if d < best:
            best = d
            best_i = i
    return best, best_i


def _uv_to_tws_twd(u: float, v: float) -> tuple[float, float]:
    """(u east, v north) m/s → (TWS kt, wind-FROM deg true).

    Same math as ``performance._uv_to_tws_twd`` (replicated tiny helper
    per house convention).
    """
    speed_ms = math.hypot(u, v)
    if speed_ms < 1e-6:
        return 0.0, 0.0
    dir_to = (math.degrees(math.atan2(u, v)) + 360.0) % 360.0
    return speed_ms / _KT_TO_MS, (dir_to + 180.0) % 360.0


# ─── Polar target angles ─────────────────────────────────────────────────


def target_twa(polar, tws_kt: float, *, upwind: bool) -> Optional[float]:
    """Best-VMG true wind angle at this TWS, from the polar table.

    Upwind: TWA in [30, 75] maximising speed·cos(TWA).
    Downwind: TWA in [120, 178] maximising −speed·cos(TWA).
    2° scan — plenty against a polar that's interpolated anyway.
    Returns None when the polar gives no positive speed in the range
    (calm / table edge).
    """
    lo, hi = (30.0, 75.0) if upwind else (120.0, 178.0)
    best_twa: Optional[float] = None
    best_vmg = 0.0
    a = lo
    while a <= hi:
        spd = float(polar.boat_speed(a, tws_kt))
        vmg = spd * math.cos(math.radians(a))
        score = vmg if upwind else -vmg
        if score > best_vmg:
            best_vmg = score
            best_twa = a
        a += 2.0
    return best_twa


# ─── Maneuver-class detectors ────────────────────────────────────────────


def detect_planned_maneuver(
    track: list[dict],
    route_coords: Optional[Sequence[tuple[float, float]]],
    now: datetime,
) -> Optional[CallCandidate]:
    """Announce the active plan's next tack/gybe when its ETA enters
    the announce window. Pure plan-vs-position math — the cheapest
    proactive call in the system.
    """
    if not track or not route_coords or len(route_coords) < 3:
        return None
    last = track[-1]
    sog = last.get("sog_kts")
    if sog is None or sog < _MIN_SOG_KT:
        return None

    xte, seg_i = _xte_to_polyline_m(last["lat"], last["lon"], route_coords)
    if xte > ON_PLAN_MAX_XTE_M:
        return None  # off the plan — divergence detector's territory

    # Walk forward from the nearest segment accumulating distance until
    # the route's heading changes by more than the turn threshold.
    dist_m = 0.0
    prev_brg: Optional[float] = None
    for i in range(seg_i, len(route_coords) - 1):
        lon1, lat1 = route_coords[i]
        lon2, lat2 = route_coords[i + 1]
        brg = _bearing_deg(lat1, lon1, lat2, lon2)
        if prev_brg is not None:
            turn = _ang_diff(brg, prev_brg)
            if abs(turn) >= TURN_THRESHOLD_DEG:
                eta_s = dist_m / (sog * _KT_TO_MS)
                if ANNOUNCE_MIN_LEAD_S <= eta_s <= ANNOUNCE_MAX_LEAD_S:
                    return CallCandidate(
                        call_type="planned_maneuver",
                        call_class="maneuver",
                        eta=now + timedelta(seconds=eta_s),
                        diagnosis={
                            "turn_deg": round(turn, 1),
                            "direction": "starboard" if turn > 0 else "port",
                            "distance_m": round(dist_m),
                            "eta_s": round(eta_s),
                        },
                    )
                return None  # next turn outside the window — stay quiet
        prev_brg = brg
        dist_m += _haversine_m(lat1, lon1, lat2, lon2)
        if dist_m / (sog * _KT_TO_MS) > ANNOUNCE_MAX_LEAD_S:
            return None  # everything further is beyond the window
    return None


def detect_layline(
    track: list[dict],
    next_mark: Optional[dict],
    forecast,
    polar,
    now: datetime,
) -> Optional[CallCandidate]:
    """Project the crossing of the opposite-tack layline to the next
    mark; announce when the crossing enters the window.

    Only meaningful beating upwind (TWA below UPWIND_TWA_MAX_DEG).
    The layline is the line through the mark at the opposite tack's
    best-VMG course; we close it at the perpendicular component of
    our current velocity.
    """
    if not track or not next_mark:
        return None
    last = track[-1]
    sog, cog = last.get("sog_kts"), last.get("cog_deg")
    if sog is None or cog is None or sog < _MIN_SOG_KT:
        return None
    uv = forecast.sample(last["lat"], last["lon"], now) if forecast else None
    if uv is None:
        return None
    tws, twd = _uv_to_tws_twd(uv[0], uv[1])
    if tws < 0.5:
        return None
    twa = abs(_ang_diff(cog, twd))
    if twa > UPWIND_TWA_MAX_DEG:
        return None  # not beating — laylines don't apply

    opt = target_twa(polar, tws, upwind=True)
    if opt is None:
        return None

    # Wind relative to bow: positive ⇒ wind from starboard ⇒ starboard tack.
    rel = _ang_diff(twd, cog)
    on_starboard = rel > 0
    # Opposite tack's course over ground (≈ heading; leeway ignored v1).
    opp_course = (twd + opt) % 360.0 if on_starboard else (twd - opt) % 360.0

    # Signed perpendicular distance from boat to the layline through the
    # mark with direction opp_course, and our closing speed toward it.
    mean_lat = math.radians((last["lat"] + next_mark["lat"]) / 2.0)
    bx = math.radians(next_mark["lon"] - last["lon"]) * math.cos(mean_lat) * _EARTH_R_M
    by = math.radians(next_mark["lat"] - last["lat"]) * _EARTH_R_M
    # Unit normal to the layline direction.
    th = math.radians(opp_course)
    dxu, dyu = math.sin(th), math.cos(th)          # course unit vector (E, N)
    nx, ny = -dyu, dxu                              # left normal
    dist_to_line = bx * nx + by * ny                # signed, metres
    vc = math.radians(cog)
    vx = sog * _KT_TO_MS * math.sin(vc)
    vy = sog * _KT_TO_MS * math.cos(vc)
    closing = vx * (nx if dist_to_line > 0 else -nx) + vy * (
        ny if dist_to_line > 0 else -ny
    )
    if closing <= 0.05:
        return None  # not converging on the layline
    eta_s = abs(dist_to_line) / closing
    if not (ANNOUNCE_MIN_LEAD_S <= eta_s <= ANNOUNCE_MAX_LEAD_S):
        return None
    return CallCandidate(
        call_type="layline",
        call_class="maneuver",
        eta=now + timedelta(seconds=eta_s),
        diagnosis={
            "tack": "starboard" if on_starboard else "port",
            "layline_for": "port" if on_starboard else "starboard",
            "mark_label": next_mark.get("label"),
            "distance_to_layline_m": round(abs(dist_to_line)),
            "eta_s": round(eta_s),
            "target_twa_deg": round(opt, 1),
            "tws_kt": round(tws, 1),
        },
    )


def detect_forecast_shift(
    track: list[dict], forecast, now: datetime,
) -> Optional[CallCandidate]:
    """Purely predictive: compare forecast wind direction at the boat's
    position now vs the look-ahead horizons; announce the first horizon
    where the shift clears the threshold.
    """
    if not track or forecast is None:
        return None
    last = track[-1]
    uv_now = forecast.sample(last["lat"], last["lon"], now)
    if uv_now is None:
        return None
    tws_now, twd_now = _uv_to_tws_twd(uv_now[0], uv_now[1])
    if tws_now < 0.5:
        return None
    for horizon_s in SHIFT_HORIZONS_S:
        t = now + timedelta(seconds=horizon_s)
        uv = forecast.sample(last["lat"], last["lon"], t)
        if uv is None:
            continue
        tws_then, twd_then = _uv_to_tws_twd(uv[0], uv[1])
        delta = _ang_diff(twd_then, twd_now)
        if abs(delta) >= SHIFT_THRESHOLD_DEG:
            return CallCandidate(
                call_type="forecast_shift",
                call_class="maneuver",
                eta=t,
                diagnosis={
                    "twd_now_deg": round(twd_now),
                    "twd_then_deg": round(twd_then),
                    "shift_deg": round(delta, 1),
                    "shift_direction": "right" if delta > 0 else "left",
                    "tws_now_kt": round(tws_now, 1),
                    "tws_then_kt": round(tws_then, 1),
                    "horizon_s": round(horizon_s),
                },
            )
    return None


# ─── Coaching-class detectors ────────────────────────────────────────────


def _window(evals: list[dict], now: datetime, span_s: float) -> list[dict]:
    cutoff = now - timedelta(seconds=span_s)
    return [e for e in evals if e.get("t") is not None and e["t"] >= cutoff]


def _cog_swing_deg(track: list[dict], now: datetime, span_s: float) -> float:
    cutoff = now - timedelta(seconds=span_s)
    cogs = [p["cog_deg"] for p in track
            if p.get("cog_deg") is not None and p["t"] >= cutoff]
    if len(cogs) < 2:
        return 0.0
    base = cogs[0]
    return max(abs(_ang_diff(c, base)) for c in cogs)


def detect_pinching(
    evals: list[dict],
    track: list[dict],
    polar,
    now: datetime,
) -> Optional[CallCandidate]:
    """Sustained TWA deviation from the polar's best-VMG upwind angle.

    Pinching (TWA below target − PINCH_DEG) or sailing low (TWA above
    target + SAIL_LOW_DEG with VMG suffering). GPS-only — ships in v1a.
    """
    win = _window(evals, now, ANGLE_SUSTAIN_S)
    up = [e for e in win if e.get("twa") is not None
          and e["twa"] <= UPWIND_TWA_MAX_DEG]
    if len(up) < _MIN_EVAL_SAMPLES:
        return None
    if _cog_swing_deg(track, now, ANGLE_SUSTAIN_S) > MANEUVER_COG_SWING_DEG:
        return None  # mid-tack — not a trim problem

    mean_twa = sum(e["twa"] for e in up) / len(up)
    mean_tws = sum(e["tws_kts"] for e in up) / len(up)
    opt = target_twa(polar, mean_tws, upwind=True)
    if opt is None:
        return None

    if mean_twa < opt - PINCH_DEG:
        delta = opt - mean_twa
        return CallCandidate(
            call_type="pinching",
            call_class="coaching",
            diagnosis={
                "mode": "pinching",
                "mean_twa_deg": round(mean_twa, 1),
                "target_twa_deg": round(opt, 1),
                "delta_deg": round(delta, 1),
                "tws_kt": round(mean_tws, 1),
            },
            adjustments=(f"foot off — bear away about {round(delta)}°",
                         "ease sheets a touch as you do"),
        )
    vmg_ratios = [e["vmg_ratio"] for e in up if e.get("vmg_ratio") is not None]
    mean_vmg = (sum(vmg_ratios) / len(vmg_ratios)) if vmg_ratios else None
    if mean_twa > opt + SAIL_LOW_DEG and mean_vmg is not None and mean_vmg < 0.9:
        delta = mean_twa - opt
        return CallCandidate(
            call_type="pinching",
            call_class="coaching",
            diagnosis={
                "mode": "sailing_low",
                "mean_twa_deg": round(mean_twa, 1),
                "target_twa_deg": round(opt, 1),
                "delta_deg": round(delta, 1),
                "tws_kt": round(mean_tws, 1),
                "vmg_ratio": round(mean_vmg, 2),
            },
            adjustments=(f"head up about {round(delta)}° toward target",
                         "trim on as you come up"),
        )
    return None


def detect_over_heel(
    heel_stat: Optional[dict],
    evals: list[dict],
    boat_class: Optional[str],
    now: datetime,
) -> Optional[CallCandidate]:
    """Sustained heel beyond the boat's band. v1c-gated at the PIPELINE
    level (needs IMU upload + the validation race); the detector itself
    is complete and unit-tested now.
    """
    if not heel_stat or not heel_stat.get("mount_ok"):
        return None
    win = _window(evals, now, ANGLE_SUSTAIN_S)
    if not win:
        return None
    latest = win[-1]
    tws = latest.get("tws_kts")
    twa = latest.get("twa")
    if tws is None or twa is None:
        return None
    band = band_for(boat_class, tws, twa)
    if band is None:
        return None
    heel = heel_stat["median_abs_deg"]
    if heel <= band.band_hi_deg:
        return None
    return CallCandidate(
        call_type="over_heel",
        call_class="coaching",
        diagnosis={
            "median_heel_deg": heel,
            "band_lo_deg": band.band_lo_deg,
            "band_hi_deg": band.band_hi_deg,
            "tws_kt": round(tws, 1),
            "twa_deg": round(twa, 1),
        },
        adjustments=band.adjustments,
    )


def detect_off_pace(
    evals: list[dict], track: list[dict], now: datetime,
) -> Optional[CallCandidate]:
    """Sustained speed-ratio deficit not explained by a maneuver.

    The pipeline only reaches this when pinching/over-heel didn't fire
    (priority order), so this is the 'something's slow and it isn't
    the angle or the heel' catch-all.
    """
    win = [e for e in _window(evals, now, OFF_PACE_SUSTAIN_S)
           if e.get("speed_ratio") is not None]
    if len(win) < _MIN_EVAL_SAMPLES:
        return None
    if _cog_swing_deg(track, now, OFF_PACE_SUSTAIN_S) > MANEUVER_COG_SWING_DEG:
        return None
    mean_ratio = sum(e["speed_ratio"] for e in win) / len(win)
    if mean_ratio >= OFF_PACE_RATIO:
        return None
    latest = win[-1]
    return CallCandidate(
        call_type="off_pace",
        call_class="coaching",
        diagnosis={
            "mean_speed_ratio": round(mean_ratio, 2),
            "target_kts": latest.get("target_kts"),
            "actual_kts": latest.get("actual_kts"),
            "tws_kt": latest.get("tws_kts"),
            "twa_deg": latest.get("twa"),
            "window_s": round(OFF_PACE_SUSTAIN_S),
        },
        adjustments=("check sail trim against targets",
                     "look for better pressure nearby"),
    )


def detect_plan_divergence(
    track: list[dict],
    route_coords: Optional[Sequence[tuple[float, float]]],
) -> Optional[CallCandidate]:
    """Cross-track error vs the active plan beyond threshold. The call
    says the plan needs attention — recomputation is the better-route
    worker's job, not this detector's.
    """
    if not track or not route_coords or len(route_coords) < 2:
        return None
    last = track[-1]
    xte, _ = _xte_to_polyline_m(last["lat"], last["lon"], route_coords)
    if xte <= DIVERGENCE_XTE_M:
        return None
    return CallCandidate(
        call_type="plan_divergence",
        call_class="coaching",
        diagnosis={"xte_m": round(xte)},
        adjustments=("work back toward the planned line",
                     "or recompute the route from here"),
    )


# ─── Runner ──────────────────────────────────────────────────────────────


def run_detectors(
    *,
    track: list[dict],
    evals: list[dict],
    forecast,
    polar,
    route_coords: Optional[Sequence[tuple[float, float]]],
    next_mark: Optional[dict],
    heel_stat: Optional[dict],
    boat_class: Optional[str],
    now: datetime,
    include_heel: bool = False,
) -> list[CallCandidate]:
    """Run every detector; return candidates sorted by priority
    (best first). ``include_heel`` is the v1c gate — False until the
    heel pipeline is validated on the water.
    """
    candidates = [
        detect_planned_maneuver(track, route_coords, now),
        detect_layline(track, next_mark, forecast, polar, now),
        detect_forecast_shift(track, forecast, now),
        detect_over_heel(heel_stat, evals, boat_class, now)
        if include_heel else None,
        detect_pinching(evals, track, polar, now),
        detect_off_pace(evals, track, now),
        detect_plan_divergence(track, route_coords),
    ]
    found = [c for c in candidates if c is not None]
    found.sort(key=lambda c: c.priority)
    return found
