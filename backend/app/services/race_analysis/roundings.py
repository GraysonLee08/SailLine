"""Section 1.6 — mark-rounding execution.

Per mark pass: mean smoothed SOG over [−45 s, −10 s] in vs
[+10 s, +45 s] out, rounding_loss_pct = 1 − sog_out/sog_in, plus time
spent within 3 boatlengths of the mark. The start pass (mark_index 0)
is skipped — crossing a line is not a rounding.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.services.race_analysis.geo import haversine_m
from app.services.race_analysis.preprocess import AnalysisPoint, mean_sog_in_window

IN_WINDOW_S = (-45.0, -10.0)
OUT_WINDOW_S = (10.0, 45.0)
NEAR_MARK_RADIUS_BL = 3.0


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


def analyze_roundings(
    points: list[AnalysisPoint],
    *,
    marks: list[dict],
    mark_passes: list[dict],
    loa_m: float,
) -> list[dict]:
    out: list[dict] = []
    n = len(marks)
    radius = NEAR_MARK_RADIUS_BL * max(loa_m, 1.0)
    for mp in mark_passes or []:
        idx = mp.get("mark_index")
        ts = _parse_ts(mp.get("ts"))
        if ts is None or not isinstance(idx, int):
            continue
        if idx == 0:
            continue  # start line, not a rounding
        is_finish = n > 1 and idx == n - 1
        mark = marks[idx] if 0 <= idx < n else None

        sog_in = mean_sog_in_window(
            points,
            ts + timedelta(seconds=IN_WINDOW_S[0]),
            ts + timedelta(seconds=IN_WINDOW_S[1]),
        )
        sog_out = None
        if not is_finish:
            sog_out = mean_sog_in_window(
                points,
                ts + timedelta(seconds=OUT_WINDOW_S[0]),
                ts + timedelta(seconds=OUT_WINDOW_S[1]),
            )

        loss_pct: Optional[float] = None
        if sog_in and sog_in > 0.5 and sog_out is not None:
            loss_pct = 1.0 - (sog_out / sog_in)

        time_near_s: Optional[float] = None
        if mark is not None:
            try:
                m_lat, m_lon = float(mark["lat"]), float(mark["lon"])
                near = 0.0
                win = [
                    p for p in points
                    if abs((p.t - ts).total_seconds()) <= 120.0
                ]
                for a, b in zip(win, win[1:]):
                    dt = (b.t - a.t).total_seconds()
                    if dt <= 0 or dt > 10.0:
                        continue
                    if haversine_m(a.lat, a.lon, m_lat, m_lon) <= radius:
                        near += dt
                time_near_s = near
            except (KeyError, TypeError, ValueError):
                pass

        out.append({
            "mark_index": idx,
            "is_finish": is_finish,
            "t": ts.isoformat(timespec="seconds"),
            "sog_in_kts": round(sog_in, 2) if sog_in is not None else None,
            "sog_out_kts": round(sog_out, 2) if sog_out is not None else None,
            "rounding_loss_pct": round(loss_pct, 3) if loss_pct is not None else None,
            "time_within_3bl_s": (
                round(time_near_s, 1) if time_near_s is not None else None
            ),
        })
    return out


__all__ = ["analyze_roundings", "IN_WINDOW_S", "OUT_WINDOW_S", "NEAR_MARK_RADIUS_BL"]
