"""Section 1.4 — shift exploitation, upwind legs only.

Per upwind leg: mean TWD, per-point shift = angle_diff(TWD_t, leg mean).
Lifted when (starboard tack and shift > +3°) or (port tack and shift <
−3°); headed when mirrored; else neutral. A "missed shift" is > 90 s of
continuous headed sailing without a tack, reported with its start
timestamp and mean magnitude.

Sign check under the package convention (signed TWA: positive =
starboard): on starboard tack a right shift (positive) rotates the wind
aft → the boat can point up → lifted. Matches the spec's rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.services.race_analysis.geo import angle_diff, circular_mean_deg
from app.services.race_analysis.legs import Leg
from app.services.race_analysis.maneuvers import Maneuver

SHIFT_DEADBAND_DEG = 3.0
MISSED_SHIFT_MIN_S = 90.0


@dataclass
class MissedShift:
    t: datetime
    duration_s: float
    mean_magnitude_deg: float

    def to_dict(self) -> dict:
        return {
            "t": self.t.isoformat(timespec="seconds"),
            "duration_s": round(self.duration_s, 1),
            "mean_magnitude_deg": round(self.mean_magnitude_deg, 1),
        }


def analyze_shifts(
    leg: Leg,
    maneuvers: Optional[list[Maneuver]] = None,
) -> Optional[dict]:
    """Shift block for one leg. None for non-upwind legs or legs with
    no wind coverage."""
    if leg.type != "upwind":
        return None
    pts = [
        p for p in leg.points
        if p.twd_deg is not None and p.twa_deg is not None
    ]
    if len(pts) < 10:
        return None
    leg_mean = circular_mean_deg([p.twd_deg for p in pts])
    if leg_mean is None:
        return None

    tack_times = {
        m.t for m in (maneuvers or []) if m.kind == "tack"
    }

    lifted_s = headed_s = neutral_s = 0.0
    missed: list[MissedShift] = []
    headed_run_start: Optional[datetime] = None
    headed_run_mags: list[float] = []

    def _close_headed_run(end_t: datetime) -> None:
        nonlocal headed_run_start, headed_run_mags
        if headed_run_start is not None:
            dur = (end_t - headed_run_start).total_seconds()
            if dur > MISSED_SHIFT_MIN_S and headed_run_mags:
                missed.append(MissedShift(
                    t=headed_run_start,
                    duration_s=dur,
                    mean_magnitude_deg=sum(headed_run_mags) / len(headed_run_mags),
                ))
        headed_run_start = None
        headed_run_mags = []

    for a, b in zip(pts, pts[1:]):
        dt = (b.t - a.t).total_seconds()
        if dt <= 0 or dt > 10.0:
            _close_headed_run(a.t)
            continue
        shift = angle_diff(a.twd_deg, leg_mean)
        on_starboard = a.twa_deg > 0
        if (on_starboard and shift > SHIFT_DEADBAND_DEG) or (
            not on_starboard and shift < -SHIFT_DEADBAND_DEG
        ):
            state = "lifted"
        elif (on_starboard and shift < -SHIFT_DEADBAND_DEG) or (
            not on_starboard and shift > SHIFT_DEADBAND_DEG
        ):
            state = "headed"
        else:
            state = "neutral"

        if state == "lifted":
            lifted_s += dt
            _close_headed_run(a.t)
        elif state == "headed":
            headed_s += dt
            if headed_run_start is None:
                headed_run_start = a.t
            headed_run_mags.append(abs(shift))
            # A tack ends the run. If the boat was already > 90 s
            # headed it still counts as missed (slow response) — the
            # close helper applies the threshold.
            if any(a.t <= tt <= b.t for tt in tack_times):
                _close_headed_run(b.t)
        else:
            neutral_s += dt
            _close_headed_run(a.t)

    _close_headed_run(pts[-1].t)

    total = lifted_s + headed_s + neutral_s
    if total <= 0:
        return None
    return {
        "leg_mean_twd_deg": round(leg_mean, 1),
        "pct_time_lifted": round(lifted_s / total, 3),
        "pct_time_headed": round(headed_s / total, 3),
        "missed_shifts": [m.to_dict() for m in missed],
    }


__all__ = ["analyze_shifts", "MissedShift", "SHIFT_DEADBAND_DEG", "MISSED_SHIFT_MIN_S"]
