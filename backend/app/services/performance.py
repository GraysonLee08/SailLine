"""Target-Actual performance engine — actual SOG/VMG vs polar target.

Phase 3 "Target-Actual Engine" (see ``Development plan.docx``): a
server-side comparison of measured boat performance against the polar
target for the wind that was present. Two layers:

* ``evaluate_point`` — single-fix comparison. The reusable core; the
  live WS advisor will call this per-fix in a later session.
* ``compute_performance_summary`` — replay a whole track into a
  leg-bucketed, time-weighted summary. Plugged into
  ``workers.race_postprocess`` and surfaced on ``/api/races/{id}/stats``.

Pure functions, no I/O — mirrors ``app/services/heel_stats.py``. The
wind source is injected as a ``wind_sampler`` callable so the engine is
agnostic to whether the wind comes from the live forecast or the
persisted ``race_sessions.wind_snapshot`` grid.

Conventions
-----------
* TWS / TWD / TWA derive from the wind (u, v) exactly the way the
  isochrone engine derives them (``uv_to_tws_twd`` + the same TWA fold),
  so the target speed we compare against matches what routing would
  have predicted.
* ``target_kts`` is the clean polar boat speed at (TWA, TWS) — no
  wave/density derating. Derating is a routing-time concern; for
  actual-vs-target we compare against the boat's nominal polar.
* ``VMG = SOG * cos(TWA)`` — speed made good along the wind axis.
  Positive = progress to windward, negative = progress to leeward.
  ``target_vmg`` uses the polar target at the same TWA so ``vmg_ratio``
  compares like with like.
* ``speed_ratio = actual_sog / target_kts``. 1.0 = on the polar; > 1
  means sailing above polar (current assist, gust, or optimistic GPS
  SOG). ``None`` when the polar target is zero (pinching above the
  close-hauled limit) so it can't form a ratio.

VMG efficiency is only meaningful away from a beam reach — near TWA 90°
``cos(TWA) → 0`` and the ratio blows up. Beam-reach points are excluded
from the VMG aggregates (but still counted in speed-ratio aggregates).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

# Wind source: (lat, lon, valid_time) -> (u_mps, v_mps) | None.
WindSampler = Callable[[float, float, Optional[datetime]], Optional[tuple[float, float]]]


# Knots <-> m/s. Same constant as ``routing.isochrone``.
_KT_TO_MS = 0.514_444

# Cap per-sample weight so one long gap (tunnel, paused tab) can't
# dominate the time-weighted averages. Matches ``heel_stats``.
_MAX_DT_S = 5.0

# Below this TWS the polar is undefined and the boat is drifting — no
# meaningful target. Matches the isochrone engine's calm-wind skip.
_MIN_TWS_KT = 0.5

# Speed-ratio band counted as "on target" for pct_time_on_target.
_ON_TARGET_LO = 0.95
_ON_TARGET_HI = 1.05

# Exclude points within a beam-reach band from VMG aggregates, where
# cos(TWA) is too small for the ratio to be stable.
_VMG_MIN_ABS_COS = 0.30  # |cos(TWA)| >= 0.30  ⇒  TWA <= ~72.5° or >= ~107.5°


def _to_aware(value) -> Optional[datetime]:
    """Accept str | datetime | None; return a tz-aware UTC datetime.

    JSONB roundtrips give us strings; asyncpg gives us datetimes. Same
    tolerant parse used by ``heel_stats`` (replicated here to keep this
    module self-contained — see the plan's "replicate small helpers"
    decision).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    return None


def _bucket_by_leg(sample_t: datetime, mark_passes: list[dict]) -> int:
    """Return leg index for a sample. Leg 0 = before the first pass,
    leg N (with N passes so far) = after the Nth pass. Inclusive on the
    lower bound. Mirrors ``heel_stats._bucket_by_leg``.
    """
    leg = 0
    for mp in mark_passes:
        ts = _to_aware(mp.get("ts"))
        if ts is None:
            continue
        if sample_t >= ts:
            leg += 1
        else:
            break
    return leg


def _uv_to_tws_twd(u: float, v: float) -> tuple[float, float]:
    """Convert (u east, v north) m/s to (speed kt, wind-from deg).

    Replicated from ``routing.isochrone.uv_to_tws_twd`` (identical math)
    so this module stays a stdlib-only leaf — importing from the routing
    package would drag in the bathymetry/charts/GCS chain just for this
    one helper.
    """
    speed_ms = math.hypot(u, v)
    if speed_ms < 1e-6:
        return 0.0, 0.0
    dir_to = (math.degrees(math.atan2(u, v)) + 360.0) % 360.0
    dir_from = (dir_to + 180.0) % 360.0
    return speed_ms / _KT_TO_MS, dir_from


def _twa_deg(cog_deg: float, wind_from_deg: float) -> float:
    """True wind angle in [0, 180] from COG and meteorological wind-from.

    Identical to ``isochrone._twa``; replicated as a 3-line local helper
    to avoid importing a private symbol across modules.
    """
    diff = (cog_deg - wind_from_deg + 360.0) % 360.0
    if diff > 180.0:
        diff = 360.0 - diff
    return diff


def evaluate_point(
    polar,
    *,
    sog_kts: Optional[float],
    cog_deg: Optional[float],
    wind_uv: Optional[tuple[float, float]],
) -> Optional[dict]:
    """Compare one GPS fix against the polar target for the local wind.

    Returns ``None`` (point not evaluable) when:
      * ``sog_kts`` or ``cog_deg`` is missing / non-finite (desktop GPS
        with no velocity, first fixes before the device computes COG),
      * ``wind_uv`` is missing (no forecast coverage for this cell), or
      * the wind is calm (TWS below ``_MIN_TWS_KT``).

    Otherwise returns::

        {
          "twa": float,          # 0..180
          "tws_kts": float,
          "twd": float,          # meteorological wind-from, degrees true
          "target_kts": float,   # clean polar boat speed at (TWA, TWS)
          "actual_kts": float,   # measured SOG
          "speed_ratio": float | None,   # actual / target; None if target==0
          "target_vmg": float,   # target_kts * cos(TWA)
          "actual_vmg": float,   # SOG * cos(TWA)
          "vmg_ratio": float | None,     # actual_vmg / target_vmg
        }
    """
    if sog_kts is None or cog_deg is None or wind_uv is None:
        return None
    if not (isinstance(sog_kts, (int, float)) and math.isfinite(sog_kts)):
        return None
    if not (isinstance(cog_deg, (int, float)) and math.isfinite(cog_deg)):
        return None
    if sog_kts < 0:
        return None

    u, v = wind_uv
    if u is None or v is None:
        return None
    tws_kts, twd = _uv_to_tws_twd(float(u), float(v))
    if tws_kts < _MIN_TWS_KT:
        return None

    twa = _twa_deg(float(cog_deg) % 360.0, twd)
    target_kts = polar.boat_speed(twa, tws_kts)

    cos_twa = math.cos(math.radians(twa))
    actual_vmg = sog_kts * cos_twa
    target_vmg = target_kts * cos_twa

    speed_ratio = (sog_kts / target_kts) if target_kts > 0 else None
    vmg_ratio = (actual_vmg / target_vmg) if abs(target_vmg) > 1e-6 else None

    return {
        "twa": round(twa, 2),
        "tws_kts": round(tws_kts, 2),
        "twd": round(twd, 2),
        "target_kts": round(target_kts, 3),
        "actual_kts": round(float(sog_kts), 3),
        "speed_ratio": round(speed_ratio, 4) if speed_ratio is not None else None,
        "target_vmg": round(target_vmg, 3),
        "actual_vmg": round(actual_vmg, 3),
        "vmg_ratio": round(vmg_ratio, 4) if vmg_ratio is not None else None,
    }


def _weighted_mean(pairs: list[tuple[float, float]]) -> Optional[float]:
    """Mean of (value, weight) pairs. None if no positive weight."""
    total_w = sum(w for _, w in pairs)
    if total_w <= 0:
        return None
    return sum(val * w for val, w in pairs) / total_w


def compute_performance_summary(
    track_points: Iterable[dict],
    *,
    wind_sampler: WindSampler,
    polar,
    mark_passes: Optional[list[dict]] = None,
) -> Optional[dict]:
    """Reduce a GPS track + wind source + polar into a performance summary.

    Inputs
    ------
    track_points
        Iterable of ``{recorded_at, lat, lon, speed_kts, heading_deg}``.
        ``recorded_at`` may be a string or datetime. Points missing
        position, speed, or heading are dropped (they can't be scored).
    wind_sampler
        Callable ``(lat, lon, valid_time) -> (u_mps, v_mps) | None``.
        Injected so the engine is agnostic to forecast vs. snapshot.
    polar
        A loaded ``app.services.polars.Polar`` for the boat class.
    mark_passes
        ``[{mark_index, ts}, ...]`` for leg bucketing. Empty/None
        produces an empty ``by_leg`` list.

    Returns ``None`` when no points are scoreable (GPS-only without
    speed/heading, no forecast coverage, calm wind, etc.).

    Shape::

        {
          "sample_count": int,
          "avg_speed_ratio": float | None,       # actual / polar target
          "avg_vmg_efficiency": float | None,    # actual VMG / target VMG
          "pct_time_on_target": float,           # 0..1 within ±5% of polar
          "avg_target_kts": float | None,
          "avg_actual_kts": float | None,
          "by_leg": [
            { "leg_index": int, "sample_count": int,
              "avg_speed_ratio": float | None,
              "avg_vmg_efficiency": float | None }
          ]
        }
    """
    mark_passes = mark_passes or []

    rows: list[dict] = []
    for p in track_points:
        t = _to_aware(p.get("recorded_at"))
        if t is None:
            continue
        lat = p.get("lat")
        lon = p.get("lon")
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
            continue
        wind_uv = wind_sampler(float(lat), float(lon), t)
        ev = evaluate_point(
            polar,
            sog_kts=p.get("speed_kts"),
            cog_deg=p.get("heading_deg"),
            wind_uv=wind_uv,
        )
        if ev is None:
            continue
        rows.append({"t": t, "leg": _bucket_by_leg(t, mark_passes), "ev": ev})

    if not rows:
        return None

    rows.sort(key=lambda r: r["t"])

    # Per-sample weights = dt to the next sample, capped (same scheme as
    # heel_stats so a long gap can't dominate).
    weights: list[float] = []
    for i, r in enumerate(rows):
        if i + 1 < len(rows):
            dt = (rows[i + 1]["t"] - r["t"]).total_seconds()
        elif i > 0:
            dt = (r["t"] - rows[i - 1]["t"]).total_seconds()
        else:
            dt = 1.0
        if dt <= 0 or dt > _MAX_DT_S:
            dt = min(max(dt, 0.01), _MAX_DT_S)
        weights.append(dt)

    def _is_vmg_evaluable(ev: dict) -> bool:
        # Exclude beam-reach points where target_vmg ~ 0.
        if ev["vmg_ratio"] is None:
            return False
        return abs(math.cos(math.radians(ev["twa"]))) >= _VMG_MIN_ABS_COS

    # ── Top-level aggregates ──────────────────────────────────────────
    speed_pairs = [
        (r["ev"]["speed_ratio"], w)
        for r, w in zip(rows, weights)
        if r["ev"]["speed_ratio"] is not None
    ]
    vmg_pairs = [
        (r["ev"]["vmg_ratio"], w)
        for r, w in zip(rows, weights)
        if _is_vmg_evaluable(r["ev"])
    ]
    target_pairs = [(r["ev"]["target_kts"], w) for r, w in zip(rows, weights)]
    actual_pairs = [(r["ev"]["actual_kts"], w) for r, w in zip(rows, weights)]

    on_target_w = sum(
        w
        for r, w in zip(rows, weights)
        if r["ev"]["speed_ratio"] is not None
        and _ON_TARGET_LO <= r["ev"]["speed_ratio"] <= _ON_TARGET_HI
    )
    total_w = sum(weights) or 1.0

    avg_speed_ratio = _weighted_mean(speed_pairs)
    avg_vmg_eff = _weighted_mean(vmg_pairs)
    avg_target = _weighted_mean(target_pairs)
    avg_actual = _weighted_mean(actual_pairs)

    # ── Per-leg aggregates ────────────────────────────────────────────
    legs_seen: dict[int, dict] = {}
    for r, w in zip(rows, weights):
        leg = r["leg"]
        b = legs_seen.setdefault(
            leg,
            {"leg_index": leg, "_speed": [], "_vmg": [], "sample_count": 0},
        )
        b["sample_count"] += 1
        if r["ev"]["speed_ratio"] is not None:
            b["_speed"].append((r["ev"]["speed_ratio"], w))
        if _is_vmg_evaluable(r["ev"]):
            b["_vmg"].append((r["ev"]["vmg_ratio"], w))

    by_leg: list[dict] = []
    for leg_index in sorted(legs_seen.keys()):
        b = legs_seen[leg_index]
        sr = _weighted_mean(b["_speed"])
        vr = _weighted_mean(b["_vmg"])
        by_leg.append({
            "leg_index": b["leg_index"],
            "sample_count": b["sample_count"],
            "avg_speed_ratio": round(sr, 4) if sr is not None else None,
            "avg_vmg_efficiency": round(vr, 4) if vr is not None else None,
        })

    return {
        "sample_count": len(rows),
        "avg_speed_ratio": round(avg_speed_ratio, 4) if avg_speed_ratio is not None else None,
        "avg_vmg_efficiency": round(avg_vmg_eff, 4) if avg_vmg_eff is not None else None,
        "pct_time_on_target": round(on_target_w / total_w, 4),
        "avg_target_kts": round(avg_target, 3) if avg_target is not None else None,
        "avg_actual_kts": round(avg_actual, 3) if avg_actual is not None else None,
        "by_leg": by_leg,
    }


__all__ = [
    "WindSampler",
    "evaluate_point",
    "compute_performance_summary",
]
