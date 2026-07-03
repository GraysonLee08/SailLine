"""Section 1.3 — maneuver (tack/gybe) detection and cost.

Detection: a sustained sign flip of the signed TWA (≥ MIN_HOLD_S on the
new sign) with |ΔCOG| > MIN_COG_DELTA_DEG across a ±10 s window around
the flip.

Classification: |TWA| < 90° on both sides → tack; > 90° both sides →
gybe; mixed (bear-away set, round-up) → "other" — counted but excluded
from tack/gybe cost aggregates.

Cost: baseline = mean smoothed SOG over [−60 s, −15 s] before the flip;
loss = ∫ max(0, baseline − SOG) dt over [−15 s, +45 s], expressed in
boatlengths (kt·s × 0.514444 m per kt·s ÷ LOA). Windows are clamped at
the neighbouring maneuver so back-to-back tacks never double-count the
same slow water.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from app.services.race_analysis.geo import KT_TO_MS, angle_diff
from app.services.race_analysis.preprocess import AnalysisPoint

MIN_HOLD_S = 10.0
MIN_COG_DELTA_DEG = 60.0
_COG_WINDOW_S = 20.0

BASELINE_WINDOW_S = (-60.0, -15.0)
COST_WINDOW_S = (-15.0, 45.0)

# Ignore TWA sign flips when the boat is nearly head-to-wind or nearly
# dead-downwind AND the flip doesn't hold — handled by the hold check —
# but also ignore flips at negligible boatspeed (drifting at the dock).
_MIN_SOG_KT = 1.0

# Fallback when the boat has no LOA on file: 10 m (~33 ft) keelboat.
DEFAULT_LOA_M = 10.0


@dataclass
class Maneuver:
    kind: str            # "tack" | "gybe" | "other"
    t: datetime
    leg_n: Optional[int]
    from_tack: str       # "port" | "starboard"
    cog_delta_deg: Optional[float]
    loss_boatlengths: Optional[float]
    loss_kt_s: Optional[float]

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "t": self.t.isoformat(timespec="seconds"),
            "leg_n": self.leg_n,
            "from_tack": self.from_tack,
            "cog_delta_deg": (
                round(self.cog_delta_deg, 1) if self.cog_delta_deg is not None else None
            ),
            "loss_boatlengths": (
                round(self.loss_boatlengths, 2)
                if self.loss_boatlengths is not None else None
            ),
        }


def detect_maneuvers(
    points: list[AnalysisPoint],
    *,
    loa_m: float = DEFAULT_LOA_M,
    leg_bounds: Optional[list[tuple[int, datetime, datetime]]] = None,
) -> list[Maneuver]:
    """Scan the cleaned, wind-annotated track for tacks and gybes.

    ``leg_bounds`` is ``[(leg_n, start_ts, end_ts), ...]`` for tagging
    each maneuver with its leg; None leaves ``leg_n`` unset.
    """
    usable = [p for p in points if p.twa_deg is not None and p.cog_deg is not None]
    if len(usable) < 4:
        return []

    flips: list[int] = []
    for i in range(1, len(usable)):
        a, b = usable[i - 1], usable[i]
        if a.twa_deg == 0 or b.twa_deg == 0:
            continue
        if (a.twa_deg > 0) == (b.twa_deg > 0):
            continue
        if b.sog_kts is not None and b.sog_kts < _MIN_SOG_KT:
            continue
        # Sustained: every sample in the next MIN_HOLD_S keeps the new sign.
        new_sign = b.twa_deg > 0
        hold_end = b.t + timedelta(seconds=MIN_HOLD_S)
        window = [p for p in usable[i:] if p.t <= hold_end]
        if not window or window[-1].t < b.t + timedelta(seconds=MIN_HOLD_S * 0.5):
            continue
        if any(p.twa_deg is not None and (p.twa_deg > 0) != new_sign for p in window):
            continue
        # Debounce: skip if we already recorded a flip within the hold.
        if flips and (b.t - usable[flips[-1]].t).total_seconds() < MIN_HOLD_S:
            continue
        flips.append(i)

    out: list[Maneuver] = []
    for k, i in enumerate(flips):
        pivot = usable[i]
        before = _nearest_by_offset(usable, pivot.t, -_COG_WINDOW_S / 2)
        after = _nearest_by_offset(usable, pivot.t, _COG_WINDOW_S / 2)
        cog_delta: Optional[float] = None
        if before is not None and after is not None:
            cog_delta = abs(angle_diff(after.cog_deg, before.cog_deg))
            if cog_delta <= MIN_COG_DELTA_DEG:
                continue

        pre_twa = abs(usable[i - 1].twa_deg)
        post_twa = abs(pivot.twa_deg)
        if pre_twa < 90.0 and post_twa < 90.0:
            kind = "tack"
        elif pre_twa > 90.0 and post_twa > 90.0:
            kind = "gybe"
        else:
            kind = "other"

        # Clamp cost windows at the neighbouring maneuvers.
        prev_t = usable[flips[k - 1]].t if k > 0 else None
        next_t = usable[flips[k + 1]].t if k + 1 < len(flips) else None
        loss_kt_s = _integrate_loss(points, pivot.t, prev_t, next_t)
        loss_bl = (
            loss_kt_s * KT_TO_MS / max(loa_m, 1.0)
            if loss_kt_s is not None else None
        )

        leg_n: Optional[int] = None
        for n, t0, t1 in leg_bounds or []:
            if t0 <= pivot.t <= t1:
                leg_n = n
                break

        out.append(Maneuver(
            kind=kind,
            t=pivot.t,
            leg_n=leg_n,
            from_tack="port" if usable[i - 1].twa_deg < 0 else "starboard",
            cog_delta_deg=cog_delta,
            loss_boatlengths=loss_bl,
            loss_kt_s=loss_kt_s,
        ))
    return out


def _nearest_by_offset(
    usable: list[AnalysisPoint], t: datetime, offset_s: float,
) -> Optional[AnalysisPoint]:
    target = t + timedelta(seconds=offset_s)
    best: Optional[AnalysisPoint] = None
    best_dt = _COG_WINDOW_S
    for p in usable:
        dt = abs((p.t - target).total_seconds())
        if dt < best_dt:
            best, best_dt = p, dt
    return best


def _integrate_loss(
    points: list[AnalysisPoint],
    pivot_t: datetime,
    prev_maneuver_t: Optional[datetime],
    next_maneuver_t: Optional[datetime],
) -> Optional[float]:
    """∫ max(0, baseline − SOG) dt over the cost window, in kt·s."""
    b_lo = pivot_t + timedelta(seconds=BASELINE_WINDOW_S[0])
    b_hi = pivot_t + timedelta(seconds=BASELINE_WINDOW_S[1])
    if prev_maneuver_t is not None:
        cut = prev_maneuver_t + timedelta(seconds=COST_WINDOW_S[1])
        if cut > b_lo:
            b_lo = cut
    base_vals = [
        p.sog_kts for p in points
        if p.sog_kts is not None and b_lo <= p.t <= b_hi
    ]
    if len(base_vals) < 3:
        return None
    baseline = sum(base_vals) / len(base_vals)

    c_lo = pivot_t + timedelta(seconds=COST_WINDOW_S[0])
    c_hi = pivot_t + timedelta(seconds=COST_WINDOW_S[1])
    if next_maneuver_t is not None:
        cut = next_maneuver_t + timedelta(seconds=COST_WINDOW_S[0])
        if cut < c_hi:
            c_hi = cut
    win = [
        p for p in points
        if p.sog_kts is not None and c_lo <= p.t <= c_hi
    ]
    if len(win) < 2:
        return None
    loss = 0.0
    for a, b in zip(win, win[1:]):
        dt = (b.t - a.t).total_seconds()
        if dt <= 0 or dt > 10.0:
            continue
        deficit = baseline - 0.5 * (a.sog_kts + b.sog_kts)
        if deficit > 0:
            loss += deficit * dt
    return max(loss, 0.0)


def summarize_maneuvers(maneuvers: list[Maneuver]) -> dict:
    """Aggregate for the payload: overall + per-leg tack/gybe stats."""
    def _agg(kind: str, items: list[Maneuver]) -> dict:
        losses = [m.loss_boatlengths for m in items if m.loss_boatlengths is not None]
        return {
            "count": len(items),
            "mean_loss_bl": round(sum(losses) / len(losses), 2) if losses else None,
            "worst_loss_bl": round(max(losses), 2) if losses else None,
            "total_loss_bl": round(sum(losses), 2) if losses else None,
        }

    tacks = [m for m in maneuvers if m.kind == "tack"]
    gybes = [m for m in maneuvers if m.kind == "gybe"]
    by_leg: dict[int, dict] = {}
    for m in maneuvers:
        if m.leg_n is None or m.kind not in ("tack", "gybe"):
            continue
        leg = by_leg.setdefault(m.leg_n, {"tacks": [], "gybes": []})
        leg["tacks" if m.kind == "tack" else "gybes"].append(m)

    return {
        "tacks": _agg("tack", tacks),
        "gybes": _agg("gybe", gybes),
        "other_count": sum(1 for m in maneuvers if m.kind == "other"),
        "by_leg": {
            str(n): {
                "tacks": _agg("tack", d["tacks"]),
                "gybes": _agg("gybe", d["gybes"]),
            }
            for n, d in sorted(by_leg.items())
        },
    }


__all__ = [
    "Maneuver", "detect_maneuvers", "summarize_maneuvers",
    "MIN_HOLD_S", "MIN_COG_DELTA_DEG", "DEFAULT_LOA_M",
]
