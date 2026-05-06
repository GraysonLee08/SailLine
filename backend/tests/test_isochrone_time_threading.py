# backend/tests/test_isochrone_time_threading.py
"""Engine tests for the new race_start time-threading parameter.

Verifies:
  - Engine calls wind.sample with the correct valid_time per iteration
  - Backwards compat: race_start=None still works against a WindField
  - WindForecast returning None (past horizon) stops node expansion
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.services.polars import load_polar
from app.services.routing.isochrone import WindField, compute_isochrone_route
from app.services.routing.wind_forecast import WindForecast


@pytest.fixture
def polar_36_7():
    return load_polar("app/services/polars/beneteau_36_7.csv")


def _uniform_field(u: float, v: float, valid_iso: str | None = None) -> WindField:
    return WindField(
        lats=np.array([41.0, 42.0, 43.0]),
        lons=np.array([-89.0, -88.0, -87.0]),
        u=np.full((3, 3), u, dtype=np.float32),
        v=np.full((3, 3), v, dtype=np.float32),
        valid_time=valid_iso,
        source="hrrr",
    )


# ─── Backwards compat ────────────────────────────────────────────────────


def test_engine_with_no_race_start_still_works(polar_36_7):
    """Legacy single-WindField call path: race_start omitted."""
    wind = _uniform_field(0.0, -5.0)  # wind from north
    result = compute_isochrone_route(
        start=(42.25, -88.0), finish=(42.0, -88.0),
        polar=polar_36_7, wind=wind,
        dt_minutes=5.0, max_iterations=100,
    )
    assert result.reached


# ─── Time threading ──────────────────────────────────────────────────────


def test_engine_calls_wind_sample_with_iteration_aware_valid_time(polar_36_7):
    """First iteration samples at race_start; second at race_start+dt; etc."""
    sampled_times: list[datetime | None] = []

    class _RecordingWind:
        def sample(self, lat, lon, valid_time=None):
            sampled_times.append(valid_time)
            return (0.0, -5.0)  # steady wind from north

    race_start = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    compute_isochrone_route(
        start=(42.25, -88.0), finish=(42.0, -88.0),
        polar=polar_36_7, wind=_RecordingWind(),
        race_start=race_start,
        dt_minutes=5.0, max_iterations=10,
    )
    # First call should be at race_start (iteration 1 → offset 0).
    assert sampled_times[0] == race_start
    # Subsequent unique-time calls should advance by dt.
    distinct_times = sorted({t for t in sampled_times if t is not None})
    if len(distinct_times) >= 2:
        delta = distinct_times[1] - distinct_times[0]
        assert delta == timedelta(minutes=5)


def test_engine_skips_nodes_when_wind_sample_returns_none(polar_36_7):
    """A node whose wind sample is None (past horizon, off-grid) doesn't expand."""
    class _AlwaysNoneWind:
        def sample(self, lat, lon, valid_time=None):
            return None

    race_start = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    result = compute_isochrone_route(
        start=(42.25, -88.0), finish=(42.0, -88.0),
        polar=polar_36_7, wind=_AlwaysNoneWind(),
        race_start=race_start,
        dt_minutes=5.0, max_iterations=20,
    )
    # No expansion possible → reached=False, only the start node explored.
    assert result.reached is False
    assert result.nodes_explored == 1


def test_engine_with_wind_forecast_two_snapshot_interpolation(polar_36_7):
    """End-to-end: WindForecast with two snapshots, engine routes successfully."""
    a = _uniform_field(0.0, -5.0, "2026-05-05T12:00:00+00:00")
    b = _uniform_field(0.0, -5.0, "2026-05-05T18:00:00+00:00")  # 6h, not 1h
    forecast = WindForecast(snapshots=[a, b])
    race_start = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)

    result = compute_isochrone_route(
        start=(42.25, -88.0), finish=(42.0, -88.0),
        polar=polar_36_7, wind=forecast,
        race_start=race_start,
        dt_minutes=5.0, max_iterations=100,
    )
    assert result.reached


def test_wind_field_sample_accepts_valid_time_kwarg_and_ignores_it():
    """Duck-type contract — engine passes valid_time even to plain WindField."""
    wind = _uniform_field(3.0, 4.0)
    t = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    # Both forms must work and return the same value.
    assert wind.sample(42.0, -88.0) == wind.sample(42.0, -88.0, valid_time=t)