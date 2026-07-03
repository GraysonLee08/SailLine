"""Section 1.7 — leeway / current inference from COG-vs-heading drift.

Per point: ``drift = angle_diff(COG, yaw)`` after aligning IMU yaw to
the nearest GPS fix (≤ 2 s). A rolling 60 s median suppresses wave
yawing. Decomposition over upwind/reach sailing:

    mean_drift_on_tack = current_component + leeway × tack_sign

so with port/starboard means d_p, d_s:

    current_component = (d_p + d_s) / 2      (sign-stable across tacks)
    leeway            = (d_s − d_p) / 2      (flips with tack)

A cross-tack component > CURRENT_MIN_DEG suggests real current; the
set/drift estimate is deliberately coarse (drift ≈ SOG × sin(c), set ≈
mean COG rotated by the drift sign) and carries a confidence grade.
The OFS currents feed isn't wired in here yet — when it is, it slots
in as an independent sanity check, not a replacement for the inference.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from app.services.race_analysis.geo import angle_diff, circular_mean_deg
from app.services.race_analysis.legs import Leg
from app.services.race_analysis.preprocess import AnalysisPoint

IMU_ALIGN_MAX_S = 2.0
DRIFT_MEDIAN_WINDOW_S = 60.0
CURRENT_MIN_DEG = 3.0

# Drop drift samples beyond this — a 40° "drift" is a maneuver or a
# mount knock, not leeway.
_MAX_DRIFT_DEG = 30.0

# Minimum aligned samples per tack before the decomposition is trusted.
_MIN_SAMPLES_PER_TACK = 30


def _to_aware(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def align_yaw(
    points: list[AnalysisPoint],
    imu_rows: list[dict],
) -> list[dict]:
    """Nearest-≤2 s join of IMU yaw onto GPS fixes.

    Returns ``[{t, cog, twa, sog, drift}]`` where drift = COG − yaw.
    Both inputs must be time-sorted (they are — the worker orders by
    recorded_at).
    """
    out: list[dict] = []
    j = 0
    n = len(imu_rows)
    parsed = []
    for r in imu_rows:
        t = _to_aware(r.get("recorded_at"))
        yaw = r.get("yaw_deg")
        if t is None or not isinstance(yaw, (int, float)):
            continue
        parsed.append((t, float(yaw) % 360.0))
    n = len(parsed)
    for p in points:
        if p.cog_deg is None:
            continue
        while j + 1 < n and parsed[j + 1][0] <= p.t:
            j += 1
        best: Optional[tuple[datetime, float]] = None
        for k in (j, j + 1):
            if 0 <= k < n:
                cand = parsed[k]
                if best is None or abs((cand[0] - p.t).total_seconds()) < abs(
                    (best[0] - p.t).total_seconds()
                ):
                    best = cand
        if best is None or abs((best[0] - p.t).total_seconds()) > IMU_ALIGN_MAX_S:
            continue
        drift = angle_diff(p.cog_deg, best[1])
        if abs(drift) > _MAX_DRIFT_DEG:
            continue
        out.append({
            "t": p.t, "cog": p.cog_deg, "twa": p.twa_deg,
            "sog": p.sog_kts, "drift": drift,
        })
    return out


def _rolling_median_drift(samples: list[dict]) -> list[dict]:
    """60 s rolling median over the drift channel."""
    out: list[dict] = []
    half = DRIFT_MEDIAN_WINDOW_S / 2
    times = [s["t"] for s in samples]
    lo = 0
    hi = 0
    for i, s in enumerate(samples):
        while lo < len(samples) and (s["t"] - times[lo]).total_seconds() > half:
            lo += 1
        while hi < len(samples) and (times[hi] - s["t"]).total_seconds() <= half:
            hi += 1
        win = sorted(x["drift"] for x in samples[lo:hi])
        if not win:
            continue
        m = len(win)
        med = win[m // 2] if m % 2 else 0.5 * (win[m // 2 - 1] + win[m // 2])
        out.append({**s, "drift": med})
    return out


def analyze_leeway(
    points: list[AnalysisPoint],
    imu_rows: list[dict],
    *,
    legs: Optional[list[Leg]] = None,
) -> Optional[dict]:
    """Leeway/current block. None when IMU coverage is too thin."""
    aligned = align_yaw(points, imu_rows)
    if not aligned:
        return None
    smoothed = _rolling_median_drift(aligned)

    port = [s for s in smoothed if s["twa"] is not None and s["twa"] < 0]
    stbd = [s for s in smoothed if s["twa"] is not None and s["twa"] > 0]
    if len(port) < _MIN_SAMPLES_PER_TACK or len(stbd) < _MIN_SAMPLES_PER_TACK:
        return None

    d_p = sum(s["drift"] for s in port) / len(port)
    d_s = sum(s["drift"] for s in stbd) / len(stbd)
    current_deg = (d_p + d_s) / 2.0
    leeway_deg = (d_s - d_p) / 2.0

    out: dict = {
        "mean_drift_port_deg": round(d_p, 1),
        "mean_drift_starboard_deg": round(d_s, 1),
        "leeway_deg": round(abs(leeway_deg), 1),
        "sample_count": len(smoothed),
    }

    # Per-leg leeway (upwind legs, where the decomposition is cleanest).
    by_leg: list[dict] = []
    for leg in legs or []:
        leg_samples = [s for s in smoothed if leg.start_ts <= s["t"] <= leg.end_ts]
        lp = [s["drift"] for s in leg_samples if s["twa"] is not None and s["twa"] < 0]
        ls = [s["drift"] for s in leg_samples if s["twa"] is not None and s["twa"] > 0]
        if len(lp) < 10 or len(ls) < 10:
            continue
        by_leg.append({
            "leg_n": leg.n,
            "mean_drift_port_deg": round(sum(lp) / len(lp), 1),
            "mean_drift_starboard_deg": round(sum(ls) / len(ls), 1),
        })
    if by_leg:
        out["by_leg"] = by_leg

    if abs(current_deg) > CURRENT_MIN_DEG:
        sogs = [s["sog"] for s in smoothed if s["sog"] is not None]
        mean_sog = sum(sogs) / len(sogs) if sogs else None
        drift_kts = (
            abs(mean_sog * math.sin(math.radians(current_deg)))
            if mean_sog is not None else None
        )
        # Set ≈ mean COG rotated ±90° toward the drift side. Coarse by
        # design — flagged via confidence.
        mean_cog = circular_mean_deg([s["cog"] for s in smoothed])
        set_deg = (
            (mean_cog + (90.0 if current_deg > 0 else -90.0)) % 360.0
            if mean_cog is not None else None
        )
        spread = _drift_spread(smoothed)
        confidence = "low"
        if spread is not None and len(smoothed) > 300:
            confidence = "high" if spread < 3.0 else ("med" if spread < 6.0 else "low")
        out["current_inference"] = {
            "cross_tack_component_deg": round(current_deg, 1),
            "estimated_set_deg": round(set_deg) if set_deg is not None else None,
            "estimated_drift_kts": (
                round(drift_kts, 2) if drift_kts is not None else None
            ),
            "confidence": confidence,
        }
    return out


def _drift_spread(samples: list[dict]) -> Optional[float]:
    """Std-dev of the smoothed drift — proxy for inference stability."""
    if len(samples) < 2:
        return None
    vals = [s["drift"] for s in samples]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(var)


__all__ = [
    "align_yaw", "analyze_leeway",
    "IMU_ALIGN_MAX_S", "CURRENT_MIN_DEG",
]
