"""Sanity tests for the isochrone engine.

Not a full coverage suite — just enough to catch a regression that would
turn the magenta line on the map into garbage.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.services.polars import load_polar
from app.services.routing.isochrone import (
    WindField,
    bearing_deg,
    compute_isochrone_route,
    haversine_m,
    project,
    uv_to_tws_twd,
)


# ─── Geometry primitives ─────────────────────────────────────────────────


def test_haversine_chicago_to_milwaukee():
    # ~70 nm
    d_m = haversine_m(41.88, -87.62, 43.04, -87.91)
    d_nm = d_m / 1852.0
    assert 65 < d_nm < 75


def test_bearing_due_north():
    b = bearing_deg(42.0, -87.6, 43.0, -87.6)
    assert abs(b) < 1.0 or abs(b - 360.0) < 1.0


def test_project_round_trip():
    lat, lon = 42.0, -87.6
    lat2, lon2 = project(lat, lon, heading_deg_=90.0, distance_m=10_000.0)
    # Eastward 10 km should leave latitude essentially unchanged
    assert abs(lat2 - lat) < 0.01
    # Distance should match
    assert abs(haversine_m(lat, lon, lat2, lon2) - 10_000.0) < 50.0


def test_uv_to_tws_twd_southerly():
    # Wind FROM south = blowing TOWARD north = positive v
    tws_kts, twd = uv_to_tws_twd(0.0, 5.0)
    assert abs(tws_kts - 5.0 / 0.5144) < 0.01
    assert abs(twd - 180.0) < 0.1


# ─── Wind field ─────────────────────────────────────────────────────────


def _uniform_wind(u_ms: float, v_ms: float) -> WindField:
    lats = np.array([41.0, 42.0, 43.0])
    lons = np.array([-89.0, -88.0, -87.0])
    u = np.full((3, 3), u_ms)
    v = np.full((3, 3), v_ms)
    return WindField(lats=lats, lons=lons, u=u, v=v)


def test_wind_field_bilerp():
    wind = _uniform_wind(0.0, 5.0)
    uv = wind.sample(42.5, -87.5)
    assert uv == pytest.approx((0.0, 5.0))


def test_wind_field_outside_returns_none():
    wind = _uniform_wind(0.0, 5.0)
    assert wind.sample(50.0, -87.5) is None


# ─── Engine ─────────────────────────────────────────────────────────────


@pytest.fixture
def polar_36_7():
    return load_polar(
        # Tests run from backend/ thanks to pytest.ini's pythonpath = .
        "app/services/polars/beneteau_36_7.csv"
    )


def test_downwind_run_reaches_finish(polar_36_7):
    """Wind from north, finish to the south — boat should reach quickly."""
    wind = _uniform_wind(0.0, -5.0)  # v < 0 → wind FROM north
    # ~15 nm dead downwind. At ~4kt (TWA 180, TWS 10) that's ~4 hours.
    result = compute_isochrone_route(
        start=(42.25, -88.0),
        finish=(42.0, -88.0),
        polar=polar_36_7,
        wind=wind,
        dt_minutes=5.0,
        max_iterations=100,
    )
    assert result.reached
    assert result.tack_count == 0  # straight downwind, no maneuvers


def test_upwind_beat_produces_tacks(polar_36_7):
    """Wind from south, finish to the south — boat must tack upwind."""
    # Use 6 m/s southerly so polar yields meaningful upwind speed
    wind = _uniform_wind(0.0, 6.0)
    result = compute_isochrone_route(
        start=(42.0, -88.0),
        finish=(41.5, -88.0),  # ~30 nm dead upwind
        polar=polar_36_7,
        wind=wind,
        dt_minutes=5.0,
        max_iterations=200,
    )
    # Should reach (or get close) and have at least one tack
    assert result.tack_count >= 1
    # Total time materially longer than a downwind run of the same distance
    assert result.total_minutes > 60
