"""Pure-function tests for app/services/heel_stats.py.

No DB, no Anthropic, no Redis. Hand-crafted IMU + calibration +
mark-pass inputs exercise the calibration history, time weighting, leg
bucketing, and edge cases.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.heel_stats import compute_heel_summary


def _t(offset_s: float) -> datetime:
    """Helper: build a UTC datetime offset_s seconds after a fixed
    reference. Reference is arbitrary; only relative offsets matter."""
    base = datetime(2026, 5, 16, 18, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_s)


def _imu(t_offset: float, heel: float, pitch: float = 0.0, yaw: float = 0.0) -> dict:
    return {
        "recorded_at": _t(t_offset),
        "heel_deg": heel,
        "pitch_deg": pitch,
        "yaw_deg": yaw,
    }


# ─── empty / invalid inputs ──────────────────────────────────────────


def test_returns_none_on_empty_input():
    assert compute_heel_summary([]) is None


def test_returns_none_when_all_samples_invalid():
    samples = [
        {"recorded_at": _t(0), "heel_deg": None, "pitch_deg": None},
        {"recorded_at": None, "heel_deg": 5.0, "pitch_deg": 0.0},
        {"recorded_at": _t(2), "heel_deg": "nope", "pitch_deg": 0.0},
    ]
    assert compute_heel_summary(samples) is None


def test_tolerates_string_timestamps():
    samples = [
        {
            "recorded_at": "2026-05-16T18:00:00+00:00",
            "heel_deg": 10.0,
            "pitch_deg": 0.0,
            "yaw_deg": 0.0,
        },
        {
            "recorded_at": "2026-05-16T18:00:01+00:00",
            "heel_deg": 12.0,
            "pitch_deg": 0.0,
            "yaw_deg": 0.0,
        },
    ]
    out = compute_heel_summary(samples)
    assert out is not None
    assert out["sample_count"] == 2
    assert out["max_heel_abs_deg"] == pytest.approx(12.0)


def test_tolerates_z_suffix_iso():
    """Python < 3.11 chokes on the trailing Z without our shim."""
    samples = [
        {
            "recorded_at": "2026-05-16T18:00:00Z",
            "heel_deg": 8.0,
            "pitch_deg": 0.0,
            "yaw_deg": 0.0,
        },
    ]
    out = compute_heel_summary(samples)
    assert out is not None
    assert out["sample_count"] == 1


# ─── basic stats ─────────────────────────────────────────────────────


def test_max_abs_and_signed_max_pick_sides():
    """Signed max should reflect the highest-magnitude side, even when
    that side is negative."""
    samples = [
        _imu(0, 10.0),
        _imu(1, 25.0),
        _imu(2, -27.0),  # bigger magnitude on port
        _imu(3, 5.0),
    ]
    out = compute_heel_summary(samples)
    assert out is not None
    assert out["max_heel_abs_deg"] == pytest.approx(27.0)
    assert out["max_heel_deg"] == pytest.approx(-27.0)


def test_avg_heel_abs_is_time_weighted():
    """A long stretch at one value should weigh more than a brief spike."""
    # 10s at 5°, 1s at 50° → weighted avg ~ (5*10 + 50*1)/11 ≈ 9.1
    samples = [_imu(s, 5.0) for s in range(11)]  # 5° at t=0..10
    samples.append(_imu(11, 50.0))               # 50° spike at t=11
    out = compute_heel_summary(samples)
    assert out is not None
    # Per the spec, last sample inherits previous dt (1s). 12 samples
    # × ~1s each. Mean of [5*11, 50*1] / 12 ≈ 8.75. Acceptable to be
    # close to that range.
    assert 8.0 < out["avg_heel_abs_deg"] < 10.0


def test_pct_time_heeled_thresholds():
    """Half-and-half input should land near 0.5 for the appropriate bucket."""
    # 10 samples at 5° (below 10°), 10 samples at 25° (above 20°)
    samples = []
    for s in range(10):
        samples.append(_imu(s, 5.0))
    for s in range(10, 20):
        samples.append(_imu(s, 25.0))
    out = compute_heel_summary(samples)
    assert out is not None
    # Last sample inherits prior dt, so weights are essentially uniform.
    assert 0.45 < out["pct_time_heeled_gt_10"] < 0.55
    assert 0.45 < out["pct_time_heeled_gt_20"] < 0.55


def test_max_pitch_independent_of_heel():
    samples = [
        _imu(0, 5.0, pitch=2.0),
        _imu(1, 5.0, pitch=-9.0),  # max abs pitch
        _imu(2, 5.0, pitch=4.0),
    ]
    out = compute_heel_summary(samples)
    assert out is not None
    assert out["max_pitch_abs_deg"] == pytest.approx(9.0)


# ─── calibration application ──────────────────────────────────────────


def test_single_calibration_applied_to_all_samples():
    cal = [{
        "captured_at": _t(0),
        "heel_zero_offset_deg": 3.0,
        "pitch_zero_offset_deg": -1.0,
    }]
    samples = [
        _imu(1, 13.0, pitch=4.0),  # corrected heel 10°
        _imu(2, 23.0, pitch=4.0),  # corrected heel 20°
    ]
    out = compute_heel_summary(samples, calibrations=cal)
    assert out is not None
    assert out["max_heel_abs_deg"] == pytest.approx(20.0)
    # Pitch also offset: 4 - (-1) = 5°
    assert out["max_pitch_abs_deg"] == pytest.approx(5.0)


def test_calibration_history_uses_latest_applicable():
    """A second calibration captured later should apply only to samples
    after its capture time."""
    cal = [
        {
            "captured_at": _t(0),
            "heel_zero_offset_deg": 0.0,
            "pitch_zero_offset_deg": 0.0,
        },
        {
            "captured_at": _t(10),
            "heel_zero_offset_deg": 5.0,
            "pitch_zero_offset_deg": 0.0,
        },
    ]
    samples = [
        _imu(1, 10.0),    # before second cal → corrected 10
        _imu(11, 10.0),   # after second cal → corrected 5
    ]
    out = compute_heel_summary(samples, calibrations=cal)
    assert out is not None
    assert out["max_heel_abs_deg"] == pytest.approx(10.0)


def test_calibration_before_any_sample_is_ignored_until_sample_time():
    """A calibration captured AFTER all samples shouldn't apply to those
    samples — we'd corrupt the offset history otherwise."""
    cal = [{
        "captured_at": _t(100),
        "heel_zero_offset_deg": 50.0,
        "pitch_zero_offset_deg": 0.0,
    }]
    samples = [_imu(1, 12.0)]
    out = compute_heel_summary(samples, calibrations=cal)
    assert out is not None
    # No offset applied; reading stays at 12°.
    assert out["max_heel_abs_deg"] == pytest.approx(12.0)


# ─── leg bucketing ───────────────────────────────────────────────────


def test_by_leg_buckets_by_mark_passes():
    passes = [
        {"mark_index": 0, "ts": _t(10)},
        {"mark_index": 1, "ts": _t(20)},
    ]
    samples = [
        _imu(1, 5.0),    # leg 0
        _imu(5, 8.0),    # leg 0
        _imu(15, 22.0),  # leg 1
        _imu(25, 15.0),  # leg 2 (after final pass)
    ]
    out = compute_heel_summary(samples, mark_passes=passes)
    assert out is not None
    by_leg = {l["leg_index"]: l for l in out["by_leg"]}
    assert 0 in by_leg and 1 in by_leg and 2 in by_leg
    assert by_leg[0]["max_heel_abs_deg"] == pytest.approx(8.0)
    assert by_leg[1]["max_heel_abs_deg"] == pytest.approx(22.0)
    assert by_leg[2]["max_heel_abs_deg"] == pytest.approx(15.0)


def test_by_leg_empty_when_no_passes():
    samples = [_imu(0, 10.0), _imu(1, 15.0)]
    out = compute_heel_summary(samples, mark_passes=[])
    assert out is not None
    # All samples land in leg 0 — one bucket.
    assert len(out["by_leg"]) == 1
    assert out["by_leg"][0]["leg_index"] == 0


def test_by_leg_tolerates_string_pass_ts():
    """``mark_passes`` come from JSONB and `ts` is typically a string."""
    passes = [{"mark_index": 0, "ts": "2026-05-16T18:00:10+00:00"}]
    samples = [
        _imu(1, 5.0),    # leg 0
        _imu(15, 12.0),  # leg 1
    ]
    out = compute_heel_summary(samples, mark_passes=passes)
    assert out is not None
    by_leg = {l["leg_index"]: l for l in out["by_leg"]}
    assert by_leg[0]["max_heel_abs_deg"] == pytest.approx(5.0)
    assert by_leg[1]["max_heel_abs_deg"] == pytest.approx(12.0)


# ─── gap handling ────────────────────────────────────────────────────


def test_long_gap_does_not_dominate_average():
    """If a single sample has a multi-minute gap to the next sample,
    that one sample's weight is capped so the average isn't ruined."""
    # 1 spike sample (50° heel) followed by 30 calm samples (5° heel).
    # Without the cap, the spike would weigh 600s vs ~30s for calm
    # (avg ≈ 47.9). With _MAX_DT_S = 5.0, the spike contributes ≤5s
    # while calm contributes ~30s — avg should be pulled well below
    # the spike value.
    samples = [_imu(0, 50.0)]  # spike, then 600s gap before calm
    for i in range(30):
        samples.append(_imu(600 + i, 5.0))
    out = compute_heel_summary(samples)
    assert out is not None
    # Sanity: without the cap, avg would be > 47°. With the cap, it
    # should be much closer to calm than to spike.
    assert out["avg_heel_abs_deg"] < 15.0
