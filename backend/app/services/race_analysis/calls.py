"""Section 1.9 — tactician-call replay and compliance.

For every persisted ``tactician_calls`` row, decide whether the boat
responded within RESPONSE_WINDOW_S using deterministic rules:

    layline / planned_maneuver / plan_divergence / forecast_shift
        → a tack or gybe within the window
    pinching
        → mean |TWA| increased ≥ 3° (bow down) or smoothed SOG rose
          ≥ 0.3 kt comparing the 60 s before vs the window after
    off_pace
        → smoothed SOG rose ≥ 0.3 kt over the same comparison
    over_heel
        → not assessable from GPS alone (heel series isn't passed
          here); reported with ``responded: null``

The output is neutral data — the prompt is instructed to treat
non-responses as review items, not criticism.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.services.race_analysis.maneuvers import Maneuver
from app.services.race_analysis.preprocess import AnalysisPoint, mean_sog_in_window

RESPONSE_WINDOW_S = 120.0
_SOG_RECOVERY_KT = 0.3
_TWA_BEAR_AWAY_DEG = 3.0

_MANEUVER_CALL_TYPES = {
    "layline", "planned_maneuver", "plan_divergence", "forecast_shift",
}


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


def _mean_abs_twa(
    points: list[AnalysisPoint], t0: datetime, t1: datetime,
) -> Optional[float]:
    vals = [abs(p.twa_deg) for p in points if p.twa_deg is not None and t0 <= p.t <= t1]
    return sum(vals) / len(vals) if vals else None


def replay_calls(
    call_rows: list[dict],
    *,
    points: list[AnalysisPoint],
    maneuvers: list[Maneuver],
) -> Optional[dict]:
    """Build the 1.9 block: per-call records + per-type compliance.

    ``call_rows`` is the worker's shape: ``{created_at, call_type,
    message, eta}``. Returns None when the race had no calls.
    """
    if not call_rows:
        return None
    maneuver_times = [m.t for m in maneuvers if m.kind in ("tack", "gybe")]

    records: list[dict] = []
    for row in call_rows:
        t = _to_aware(row.get("created_at"))
        ctype = row.get("call_type")
        if t is None or not isinstance(ctype, str):
            continue
        responded = _responded(ctype, t, points, maneuver_times)
        records.append({
            "t": t.isoformat(timespec="seconds"),
            "type": ctype,
            "text": row.get("message"),
            "responded": responded,
        })

    if not records:
        return None

    by_type: dict[str, dict] = {}
    for r in records:
        b = by_type.setdefault(r["type"], {"count": 0, "responded": 0, "assessable": 0})
        b["count"] += 1
        if r["responded"] is not None:
            b["assessable"] += 1
            if r["responded"]:
                b["responded"] += 1

    compliance = {
        t: {
            "count": b["count"],
            "responded": b["responded"],
            "compliance": (
                round(b["responded"] / b["assessable"], 2)
                if b["assessable"] else None
            ),
        }
        for t, b in sorted(by_type.items())
    }
    return {"calls": records, "compliance_by_type": compliance}


def _responded(
    ctype: str,
    t: datetime,
    points: list[AnalysisPoint],
    maneuver_times: list[datetime],
) -> Optional[bool]:
    t_end = t + timedelta(seconds=RESPONSE_WINDOW_S)

    if ctype in _MANEUVER_CALL_TYPES:
        return any(t <= mt <= t_end for mt in maneuver_times)

    if ctype == "pinching":
        before_twa = _mean_abs_twa(points, t - timedelta(seconds=60), t)
        after_twa = _mean_abs_twa(points, t, t_end)
        if before_twa is not None and after_twa is not None:
            if after_twa - before_twa >= _TWA_BEAR_AWAY_DEG:
                return True
        # Fall through to the speed check — footing shows up as SOG too.
        return _sog_recovered(points, t)

    if ctype == "off_pace":
        return _sog_recovered(points, t)

    # over_heel and anything unknown: not assessable from GPS data.
    return None


def _sog_recovered(points: list[AnalysisPoint], t: datetime) -> Optional[bool]:
    before = mean_sog_in_window(points, t - timedelta(seconds=60), t)
    after = mean_sog_in_window(
        points, t + timedelta(seconds=30), t + timedelta(seconds=RESPONSE_WINDOW_S),
    )
    if before is None or after is None:
        return None
    return (after - before) >= _SOG_RECOVERY_KT


__all__ = ["replay_calls", "RESPONSE_WINDOW_S"]
