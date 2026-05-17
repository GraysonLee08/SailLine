"""Compute heel summary stats from raw IMU samples + calibration history.

Pure function. No DB, no Redis, no network. Callers (the
``race-postprocess`` Cloud Run Job) load ``imu_samples`` and
``race_calibrations`` rows for a race and pass them in. The output is
a dict shaped for two consumers:

* the AI prompt builder (``services/race_summary.build_prompt``), which
  renders it as a "Boat heel" block when present;
* future stats-view tiles that show max heel, % heeled past 20°, etc.

Calibration model
-----------------
``race_calibrations`` is a history: each row says "at ``captured_at``,
the boat was level and the phone read ``heel_zero_offset_deg`` /
``pitch_zero_offset_deg``." We treat each row as the active offset for
every IMU sample with ``recorded_at >= captured_at`` and a later
calibration has not yet superseded.

For the dock-only-zero UX we ship in v1, this collapses to "one row,
applied to every sample." But the function handles the general history
case so a future "re-zero mid-race" feature is a UI change only.

Output shape
------------
::

    {
      "sample_count":              int,    # samples in the window
      "max_heel_abs_deg":          float,
      "max_heel_deg":              float,  # signed, useful for max-side
      "avg_heel_abs_deg":          float,  # mean of |heel|, time-weighted
      "pct_time_heeled_gt_10":     float,  # 0..1
      "pct_time_heeled_gt_20":     float,  # 0..1
      "max_pitch_abs_deg":         float,
      "by_leg": [
          {
              "leg_index":         int,
              "max_heel_abs_deg":  float,
              "avg_heel_abs_deg":  float,
              "sample_count":      int,
          },
          ...
      ],
    }

``None`` is returned when there are no usable IMU samples — the caller
just skips the heel block in the prompt.

Time weighting
--------------
Each sample is weighted by ``dt_seconds`` to its successor (capped at 5s
so a stretch of dropped samples doesn't dominate the average). The
final sample inherits the previous interval. Uniform-weight is a fine
fallback when samples are evenly spaced; the cap matters for partial-
sample bursts that happen around screen sleep + wake on the web build.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional


# Cap per-sample weight to avoid one long gap dominating the average.
_MAX_DT_S = 5.0


def _to_aware(value) -> Optional[datetime]:
    """Accept str | datetime | None; return a tz-aware UTC datetime.

    JSONB roundtrips give us strings; asyncpg gives us datetimes. The
    function should tolerate both since the postprocess job assembles
    these from different sources.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        # ``fromisoformat`` doesn't tolerate the trailing ``Z`` on
        # Python < 3.11; canonicalise.
        s = value.replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    return None


def _resolve_offset(
    sample_t: datetime, calibrations: list[dict],
) -> tuple[float, float]:
    """Return (heel_off, pitch_off) for the latest calibration whose
    ``captured_at <= sample_t``. If none, returns (0, 0).

    ``calibrations`` must be the full list as loaded from the DB; this
    function does the per-sample search. For the dock-only-zero UX
    that's typically one row, so the linear search is fine. If we ever
    expect many calibrations per race we can pre-sort + binary-search.
    """
    best_t: Optional[datetime] = None
    heel_off = 0.0
    pitch_off = 0.0
    for cal in calibrations:
        cap = _to_aware(cal.get("captured_at"))
        if cap is None or cap > sample_t:
            continue
        if best_t is None or cap > best_t:
            best_t = cap
            heel_off = float(cal.get("heel_zero_offset_deg") or 0.0)
            pitch_off = float(cal.get("pitch_zero_offset_deg") or 0.0)
    return heel_off, pitch_off


def _bucket_by_leg(
    sample_t: datetime, mark_passes: list[dict],
) -> int:
    """Return leg index for a sample. Leg 0 = before the first pass,
    leg N (with N passes) = after the Nth pass. Bucketing is inclusive
    on the lower bound (a sample exactly at a pass timestamp falls into
    the next leg).
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


def compute_heel_summary(
    imu_samples: Iterable[dict],
    *,
    calibrations: Optional[list[dict]] = None,
    mark_passes: Optional[list[dict]] = None,
) -> Optional[dict]:
    """Reduce raw IMU samples + calibration history into a summary dict.

    Inputs
    ------
    imu_samples
        Iterable of ``{recorded_at, heel_deg, pitch_deg, yaw_deg}``.
        ``recorded_at`` may be a string or datetime. Other keys may be
        missing or None; we drop the sample in that case.
    calibrations
        List of ``{captured_at, heel_zero_offset_deg, pitch_zero_offset_deg}``.
        Empty/None means "no offsets applied."
    mark_passes
        List of ``{mark_index, ts}`` for leg bucketing. Empty/None
        produces an empty ``by_leg`` list — the caller may still find
        the top-level numbers useful.

    Returns ``None`` if there are zero usable samples after filtering.
    """
    calibrations = calibrations or []
    mark_passes = mark_passes or []

    # Materialise once so we can compute dt to the next sample and
    # iterate twice (once for top-level, once for legs).
    rows: list[dict] = []
    for s in imu_samples:
        t = _to_aware(s.get("recorded_at"))
        h = s.get("heel_deg")
        p = s.get("pitch_deg")
        if t is None:
            continue
        if not isinstance(h, (int, float)) or not isinstance(p, (int, float)):
            continue
        heel_off, pitch_off = _resolve_offset(t, calibrations)
        leg = _bucket_by_leg(t, mark_passes)
        rows.append({
            "t": t,
            "heel": float(h) - heel_off,
            "pitch": float(p) - pitch_off,
            "leg": leg,
        })

    if not rows:
        return None

    rows.sort(key=lambda r: r["t"])

    # Compute per-sample weights (dt to next sample, capped).
    weights = []
    for i, r in enumerate(rows):
        if i + 1 < len(rows):
            dt = (rows[i + 1]["t"] - r["t"]).total_seconds()
        elif i > 0:
            dt = (r["t"] - rows[i - 1]["t"]).total_seconds()
        else:
            dt = 1.0  # single sample — arbitrary positive weight
        if dt <= 0 or dt > _MAX_DT_S:
            dt = min(max(dt, 0.01), _MAX_DT_S)
        weights.append(dt)

    total_w = sum(weights) or 1.0

    heel_abs_vals = [abs(r["heel"]) for r in rows]
    pitch_abs_vals = [abs(r["pitch"]) for r in rows]

    max_heel_abs = max(heel_abs_vals)
    # Signed max-by-magnitude — useful for the AI to say "heeled to 28°
    # to starboard" rather than just "28°."
    max_heel_signed = max(
        (r["heel"] for r in rows), key=lambda v: abs(v),
    )
    max_pitch_abs = max(pitch_abs_vals)

    avg_heel_abs = sum(
        ha * w for ha, w in zip(heel_abs_vals, weights)
    ) / total_w

    w_gt_10 = sum(
        w for ha, w in zip(heel_abs_vals, weights) if ha > 10.0
    )
    w_gt_20 = sum(
        w for ha, w in zip(heel_abs_vals, weights) if ha > 20.0
    )

    # Per-leg buckets. We aggregate by row['leg']; output is sorted.
    legs_seen: dict[int, dict] = {}
    for r, w in zip(rows, weights):
        leg = r["leg"]
        bucket = legs_seen.setdefault(
            leg,
            {
                "leg_index": leg,
                "_heel_abs_sum_w": 0.0,
                "_w": 0.0,
                "max_heel_abs_deg": 0.0,
                "sample_count": 0,
            },
        )
        ha = abs(r["heel"])
        bucket["_heel_abs_sum_w"] += ha * w
        bucket["_w"] += w
        if ha > bucket["max_heel_abs_deg"]:
            bucket["max_heel_abs_deg"] = ha
        bucket["sample_count"] += 1

    by_leg: list[dict] = []
    for leg_index in sorted(legs_seen.keys()):
        b = legs_seen[leg_index]
        avg = b["_heel_abs_sum_w"] / (b["_w"] or 1.0)
        by_leg.append({
            "leg_index": b["leg_index"],
            "max_heel_abs_deg": round(b["max_heel_abs_deg"], 2),
            "avg_heel_abs_deg": round(avg, 2),
            "sample_count": b["sample_count"],
        })

    return {
        "sample_count": len(rows),
        "max_heel_abs_deg": round(max_heel_abs, 2),
        "max_heel_deg": round(max_heel_signed, 2),
        "avg_heel_abs_deg": round(avg_heel_abs, 2),
        "pct_time_heeled_gt_10": round(w_gt_10 / total_w, 4),
        "pct_time_heeled_gt_20": round(w_gt_20 / total_w, 4),
        "max_pitch_abs_deg": round(max_pitch_abs, 2),
        "by_leg": by_leg,
    }
