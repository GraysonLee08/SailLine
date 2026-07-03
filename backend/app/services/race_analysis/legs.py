"""Section 1.2 — leg segmentation and per-leg aggregates.

Legs are bounded by consecutive ``mark_passes`` entries (the detector
already fired the timestamps). Leg *n* runs from pass *n−1* to pass
*n*; with the start line as mark 0, leg 1 = start → first mark, which
matches ``race_stats`` numbering (leg_index 0 there = our n 1 − 1).

Classification uses the leg-mean TWD vs the rhumb bearing:
|angle to rhumb| < 50° → upwind, 50–120° → reach, > 120° → run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.services.race_analysis.geo import (
    NM_TO_M,
    angle_diff,
    bearing_deg,
    circular_mean_deg,
    haversine_m,
)
from app.services.race_analysis.preprocess import AnalysisPoint

import math

UPWIND_MAX_DEG = 50.0
RUN_MIN_DEG = 120.0


@dataclass
class Leg:
    n: int                      # 1-based; leg 1 = start → first mark
    type: str                   # "upwind" | "reach" | "run" | "unknown"
    from_mark_index: int
    to_mark_index: int
    start_ts: datetime
    end_ts: datetime
    elapsed_s: float
    distance_sailed_nm: Optional[float]
    rhumb_nm: Optional[float]
    sailed_ratio: Optional[float]
    rhumb_bearing_deg: Optional[float]
    avg_sog_kts: Optional[float]
    avg_vmg_to_mark_kts: Optional[float]
    mean_twd_deg: Optional[float]
    mean_tws_kts: Optional[float]
    points: list[AnalysisPoint] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        d = {
            "n": self.n,
            "type": self.type,
            "elapsed_s": round(self.elapsed_s, 1),
            "distance_sailed_nm": _rnd(self.distance_sailed_nm, 3),
            "rhumb_nm": _rnd(self.rhumb_nm, 3),
            "sailed_ratio": _rnd(self.sailed_ratio, 3),
            "rhumb_bearing_deg": _rnd(self.rhumb_bearing_deg, 1),
            "avg_sog_kts": _rnd(self.avg_sog_kts, 2),
            "avg_vmg_to_mark_kts": _rnd(self.avg_vmg_to_mark_kts, 2),
            "mean_twd_deg": _rnd(self.mean_twd_deg, 1),
            "mean_tws_kts": _rnd(self.mean_tws_kts, 1),
        }
        return d


def _rnd(v: Optional[float], nd: int) -> Optional[float]:
    return round(v, nd) if isinstance(v, (int, float)) else None


def _parse_ts(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        s = v.replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def segment_legs(
    points: list[AnalysisPoint],
    *,
    marks: list[dict],
    mark_passes: list[dict],
) -> list[Leg]:
    """Split the cleaned track at mark-pass timestamps.

    Requires ≥ 2 usable passes (a leg needs both boundaries). Passes
    with unparseable timestamps are skipped. Marks are looked up by the
    pass's ``mark_index`` so missed marks don't shift the mapping.
    """
    passes: list[tuple[int, datetime]] = []
    for mp in mark_passes or []:
        ts = _parse_ts(mp.get("ts"))
        idx = mp.get("mark_index")
        if ts is None or not isinstance(idx, int):
            continue
        passes.append((idx, ts))
    passes.sort(key=lambda p: p[1])
    if len(passes) < 2:
        return []

    legs: list[Leg] = []
    for i in range(1, len(passes)):
        from_idx, t0 = passes[i - 1]
        to_idx, t1 = passes[i]
        if t1 <= t0:
            continue
        leg_pts = [p for p in points if t0 <= p.t <= t1]

        from_mark = marks[from_idx] if 0 <= from_idx < len(marks) else None
        to_mark = marks[to_idx] if 0 <= to_idx < len(marks) else None

        rhumb_m: Optional[float] = None
        rhumb_brg: Optional[float] = None
        if from_mark and to_mark:
            try:
                rhumb_m = haversine_m(
                    float(from_mark["lat"]), float(from_mark["lon"]),
                    float(to_mark["lat"]), float(to_mark["lon"]),
                )
                rhumb_brg = bearing_deg(
                    float(from_mark["lat"]), float(from_mark["lon"]),
                    float(to_mark["lat"]), float(to_mark["lon"]),
                )
            except (KeyError, TypeError, ValueError):
                pass

        sailed_m: Optional[float] = None
        if len(leg_pts) >= 2:
            sailed_m = sum(
                haversine_m(a.lat, a.lon, b.lat, b.lon)
                for a, b in zip(leg_pts, leg_pts[1:])
            )

        elapsed = (t1 - t0).total_seconds()
        sogs = [p.sog_kts for p in leg_pts if p.sog_kts is not None]
        avg_sog = sum(sogs) / len(sogs) if sogs else None

        avg_vmg: Optional[float] = None
        if to_mark is not None:
            vmgs: list[float] = []
            try:
                m_lat, m_lon = float(to_mark["lat"]), float(to_mark["lon"])
            except (KeyError, TypeError, ValueError):
                m_lat = m_lon = None  # type: ignore[assignment]
            if m_lat is not None:
                for p in leg_pts:
                    if p.sog_kts is None or p.cog_deg is None:
                        continue
                    brg = bearing_deg(p.lat, p.lon, m_lat, m_lon)
                    vmgs.append(
                        p.sog_kts * math.cos(math.radians(angle_diff(p.cog_deg, brg)))
                    )
                avg_vmg = sum(vmgs) / len(vmgs) if vmgs else None

        twds = [p.twd_deg for p in leg_pts if p.twd_deg is not None]
        twss = [p.tws_kts for p in leg_pts if p.tws_kts is not None]
        mean_twd = circular_mean_deg(twds)
        mean_tws = sum(twss) / len(twss) if twss else None

        leg_type = "unknown"
        if mean_twd is not None and rhumb_brg is not None:
            off = abs(angle_diff(mean_twd, rhumb_brg))
            if off < UPWIND_MAX_DEG:
                leg_type = "upwind"
            elif off <= RUN_MIN_DEG:
                leg_type = "reach"
            else:
                leg_type = "run"

        legs.append(Leg(
            n=i,
            type=leg_type,
            from_mark_index=from_idx,
            to_mark_index=to_idx,
            start_ts=t0,
            end_ts=t1,
            elapsed_s=elapsed,
            distance_sailed_nm=(sailed_m / NM_TO_M) if sailed_m is not None else None,
            rhumb_nm=(rhumb_m / NM_TO_M) if rhumb_m is not None else None,
            sailed_ratio=(
                sailed_m / rhumb_m
                if sailed_m is not None and rhumb_m and rhumb_m > 0
                else None
            ),
            rhumb_bearing_deg=rhumb_brg,
            avg_sog_kts=avg_sog,
            avg_vmg_to_mark_kts=avg_vmg,
            mean_twd_deg=mean_twd,
            mean_tws_kts=mean_tws,
            points=leg_pts,
        ))
    return legs


__all__ = ["Leg", "segment_legs", "UPWIND_MAX_DEG", "RUN_MIN_DEG"]
