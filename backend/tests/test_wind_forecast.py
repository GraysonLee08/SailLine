# backend/tests/test_wind_forecast.py
"""Unit tests for WindForecast — time-aware wind sampling.

Pure unit tests; no Redis, no async. Covers interpolation math,
out-of-window handling, and defensive sorting.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from app.services.routing.isochrone import WindField
from app.services.routing.wind_forecast import WindForecast


def _make_field(u_val: float, v_val: float, valid_iso: str | None) -> WindField:
    """Uniform 3x3 wind field at a fixed valid_time."""
    return WindField(
        lats=np.array([41.0, 42.0, 43.0]),
        lons=np.array([-89.0, -88.0, -87.0]),
        u=np.full((3, 3), u_val, dtype=np.float32),
        v=np.full((3, 3), v_val, dtype=np.float32),
        reference_time="2026-05-05T12:00:00+00:00",
        valid_time=valid_iso,
        source="hrrr",
    )


# ─── Construction ────────────────────────────────────────────────────────


def test_requires_at_least_one_snapshot():
    with pytest.raises(ValueError, match="at least one"):
        WindForecast(snapshots=[])


def test_requires_valid_time_on_every_snapshot():
    f = _make_field(1.0, 2.0, valid_iso=None)
    with pytest.raises(ValueError, match="valid_time"):
        WindForecast(snapshots=[f])


def test_snapshots_sorted_defensively():
    later = _make_field(0.0, 0.0, "2026-05-05T13:00:00+00:00")
    earlier = _make_field(0.0, 0.0, "2026-05-05T12:00:00+00:00")
    fc = WindForecast(snapshots=[later, earlier])  # out of order
    assert fc.snapshots[0].valid_time.startswith("2026-05-05T12")
    assert fc.snapshots[1].valid_time.startswith("2026-05-05T13")


def test_t_min_t_max_reflect_extremes():
    fields = [
        _make_field(0.0, 0.0, "2026-05-05T12:00:00+00:00"),
        _make_field(0.0, 0.0, "2026-05-05T15:00:00+00:00"),
        _make_field(0.0, 0.0, "2026-05-05T13:00:00+00:00"),
    ]
    fc = WindForecast(snapshots=fields)
    assert fc.t_min == datetime(2026, 5, 5, 12, tzinfo=timezone.utc)
    assert fc.t_max == datetime(2026, 5, 5, 15, tzinfo=timezone.utc)


# ─── Sampling ────────────────────────────────────────────────────────────


def test_sample_with_no_time_uses_first_snapshot():
    """Legacy-caller path: WindForecast.sample(lat, lon) with no time."""
    a = _make_field(1.0, 2.0, "2026-05-05T12:00:00+00:00")
    b = _make_field(10.0, 20.0, "2026-05-05T14:00:00+00:00")
    fc = WindForecast(snapshots=[a, b])
    uv = fc.sample(42.0, -88.0)
    assert uv == pytest.approx((1.0, 2.0))


def test_sample_inside_window_linearly_interpolates():
    a = _make_field(0.0, 0.0, "2026-05-05T12:00:00+00:00")
    b = _make_field(10.0, 10.0, "2026-05-05T13:00:00+00:00")
    fc = WindForecast(snapshots=[a, b])
    t_mid = datetime(2026, 5, 5, 12, 30, tzinfo=timezone.utc)
    u, v = fc.sample(42.0, -88.0, t_mid)
    assert u == pytest.approx(5.0)
    assert v == pytest.approx(5.0)


def test_sample_at_exact_snapshot_time_returns_that_snapshot():
    a = _make_field(2.0, 3.0, "2026-05-05T12:00:00+00:00")
    b = _make_field(10.0, 11.0, "2026-05-05T13:00:00+00:00")
    fc = WindForecast(snapshots=[a, b])
    t = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)
    assert fc.sample(42.0, -88.0, t) == pytest.approx((10.0, 11.0))


def test_sample_before_window_returns_none():
    a = _make_field(0.0, 0.0, "2026-05-05T12:00:00+00:00")
    b = _make_field(10.0, 10.0, "2026-05-05T13:00:00+00:00")
    fc = WindForecast(snapshots=[a, b])
    t = datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc)
    assert fc.sample(42.0, -88.0, t) is None


def test_sample_after_window_returns_none():
    a = _make_field(0.0, 0.0, "2026-05-05T12:00:00+00:00")
    b = _make_field(10.0, 10.0, "2026-05-05T13:00:00+00:00")
    fc = WindForecast(snapshots=[a, b])
    t = datetime(2026, 5, 5, 14, 0, tzinfo=timezone.utc)
    assert fc.sample(42.0, -88.0, t) is None


def test_sample_outside_grid_returns_none_even_in_time_window():
    a = _make_field(0.0, 0.0, "2026-05-05T12:00:00+00:00")
    b = _make_field(10.0, 10.0, "2026-05-05T13:00:00+00:00")
    fc = WindForecast(snapshots=[a, b])
    t = datetime(2026, 5, 5, 12, 30, tzinfo=timezone.utc)
    # lat=50 is far outside the 41..43 grid
    assert fc.sample(50.0, -88.0, t) is None


def test_three_snapshot_interpolation_picks_correct_bracket():
    """Sampling at 13:30 should bracket [13:00, 14:00], not [12:00, 14:00]."""
    a = _make_field(0.0, 0.0, "2026-05-05T12:00:00+00:00")
    b = _make_field(10.0, 0.0, "2026-05-05T13:00:00+00:00")
    c = _make_field(20.0, 0.0, "2026-05-05T14:00:00+00:00")
    fc = WindForecast(snapshots=[a, b, c])
    t = datetime(2026, 5, 5, 13, 30, tzinfo=timezone.utc)
    u, _ = fc.sample(42.0, -88.0, t)
    # Linear between 10 and 20 at midpoint = 15
    assert u == pytest.approx(15.0)


def test_z_suffix_iso_time_parses():
    """NOAA timestamps sometimes come with trailing 'Z' instead of '+00:00'."""
    a = _make_field(0.0, 0.0, "2026-05-05T12:00:00Z")
    b = _make_field(10.0, 0.0, "2026-05-05T13:00:00Z")
    fc = WindForecast(snapshots=[a, b])
    t = datetime(2026, 5, 5, 12, 30, tzinfo=timezone.utc)
    u, _ = fc.sample(42.0, -88.0, t)
    assert u == pytest.approx(5.0)


def test_quality_field_round_trips():
    a = _make_field(0.0, 0.0, "2026-05-05T12:00:00+00:00")
    fc = WindForecast(snapshots=[a], quality="hrrr+gfs")
    assert fc.quality == "hrrr+gfs"