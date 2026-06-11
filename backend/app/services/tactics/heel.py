"""Sustained-heel statistic + mount-quality gate for the live tactician.

Pure functions, no I/O — mirrors ``app/services/heel_stats.py`` (which
owns the *post-race* heel summary; this module owns the *live* windowed
statistic the over-heel detector consumes).

Why a separate module instead of extending heel_stats: the live path
needs a rolling median + variance over a short window plus a
mount-quality verdict, none of which the post-race summary computes,
and the post-race path needs leg bucketing the live path doesn't.
The calibration-offset resolution helper is replicated (3 lines of
logic) per the repo's "replicate small helpers over cross-module
private imports" convention (see ``performance.py`` docstring).

Mount-quality gate (spec 2026-06-11): a loose / pocketed phone produces
attitude that swings tens of degrees sample-to-sample. Wave-driven heel
oscillation on a keelboat is real but bounded; sample-to-sample spread
beyond ``MAX_PLAUSIBLE_STDEV_DEG`` means the signal is not boat heel
and ALL heel calls must be suppressed for the session rather than risk
a confidently-wrong trim call.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional


# Rolling window the sustained statistic is computed over. Long enough
# to average wave motion (a 6 s wave period gives ~10 cycles), short
# enough that a real trim problem surfaces within a minute.
WINDOW_S: float = 60.0

# Minimum samples inside the window for the statistic to be meaningful.
# At the mobile uploader's 2 Hz decimation that's ~15 s of data.
MIN_SAMPLES: int = 30

# Sample-to-sample spread beyond which the mount is untrustworthy.
# A hard-mounted phone on a keelboat in chop shows stdev well under
# 10°; a phone loose in a pocket shows 25°+.
MAX_PLAUSIBLE_STDEV_DEG: float = 18.0


def _to_aware(value) -> Optional[datetime]:
    """str | datetime | None → tz-aware UTC datetime (tolerant)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _resolve_heel_offset(
    sample_t: datetime, calibrations: list[dict],
) -> float:
    """Latest ``heel_zero_offset_deg`` whose captured_at <= sample_t.

    Same semantics as ``heel_stats._resolve_offset`` (heel channel
    only — the live detector doesn't use pitch).
    """
    best_t: Optional[datetime] = None
    heel_off = 0.0
    for cal in calibrations or []:
        cap = _to_aware(cal.get("captured_at"))
        if cap is None or cap > sample_t:
            continue
        if best_t is None or cap > best_t:
            best_t = cap
            heel_off = float(cal.get("heel_zero_offset_deg") or 0.0)
    return heel_off


def sustained_heel(
    imu_samples: Iterable[dict],
    *,
    calibrations: Optional[list[dict]] = None,
    now: Optional[datetime] = None,
    window_s: float = WINDOW_S,
) -> Optional[dict]:
    """Windowed heel statistic for the over-heel detector.

    Parameters
    ----------
    imu_samples
        Iterable of ``{recorded_at, heel_deg}`` dicts (raw values as
        stored — calibration offsets are applied here at read time,
        matching the ``imu_samples`` table contract).
    calibrations
        ``race_calibrations`` rows for the session (may be empty).
    now
        End of the window; defaults to utcnow. Injected for tests.

    Returns
    -------
    ``None`` when there are fewer than MIN_SAMPLES samples in the
    window (no IMU permission, sparse upload, race just started).
    Otherwise::

        {
          "median_heel_deg":  float,  # signed; + = starboard rail down
          "median_abs_deg":   float,  # magnitude the band check uses
          "stdev_deg":        float,
          "sample_count":     int,
          "mount_ok":         bool,   # False ⇒ suppress ALL heel calls
        }
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=window_s)
    heels: list[float] = []
    for s in imu_samples:
        t = _to_aware(s.get("recorded_at"))
        if t is None or t < cutoff:
            continue
        raw = s.get("heel_deg")
        if raw is None:
            continue
        heels.append(float(raw) - _resolve_heel_offset(t, calibrations or []))

    if len(heels) < MIN_SAMPLES:
        return None

    med = statistics.median(heels)
    stdev = statistics.pstdev(heels)
    return {
        "median_heel_deg": round(med, 1),
        "median_abs_deg": round(abs(med), 1),
        "stdev_deg": round(stdev, 1),
        "sample_count": len(heels),
        "mount_ok": stdev <= MAX_PLAUSIBLE_STDEV_DEG,
    }
