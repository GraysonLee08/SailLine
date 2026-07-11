# backend/tests/test_isochrone_maneuver.py
"""Tests for v13 maneuver penalties + top-K bin culling.

The v12 engine had no cost for switching tacks/gybes, so two long
boards and a staircase of one-step boards took identical modeled time.
The bearing-bin culling then systematically preferred the staircase
(alternating sides hugs the direct bearing to the finish), producing
routes with a gybe every 5-minute step — unsailable in practice.

These tests count *side flips* (wind moving from one side of the boat
to the other between consecutive path headings), not the RouteResult
``tack_count``, because tack_count only registers heading changes
strictly greater than 60° — downwind gybes between hot angles
(e.g. 150° ↔ 210° under a northerly) are exactly 60° apart and were
never counted. That blind spot is how the staircase shipped.

Style follows test_isochrone_engine.py: small synthetic wind fields,
real polar CSV, pytest fixtures.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.services.polars import load_polar
from app.services.routing.isochrone import (
    WindField,
    compute_isochrone_route,
    compute_isochrone_route_multileg,
)


@pytest.fixture
def polar_36_7():
    return load_polar("app/services/polars/beneteau_36_7.csv")


def _uniform_wind(u_ms: float, v_ms: float, *, lat_min=40.0, lat_max=44.0,
                  lon_min=-90.0, lon_max=-86.0, step=0.5) -> WindField:
    lats = np.arange(lat_min, lat_max + step / 2.0, step)
    lons = np.arange(lon_min, lon_max + step / 2.0, step)
    u = np.full((len(lats), len(lons)), u_ms, dtype=np.float32)
    v = np.full((len(lats), len(lons)), v_ms, dtype=np.float32)
    return WindField(lats=lats, lons=lons, u=u, v=v)


def _count_side_flips(headings: list[float], wind_from_deg: float) -> int:
    """Number of times the wind crosses from one side of the boat to the
    other along a heading sequence. Uniform-wind helper — the real
    engine samples wind per node, but these tests use constant fields.
    """
    def side(h: float) -> int:
        rel = (h - wind_from_deg + 360.0) % 360.0
        if rel < 1e-9 or abs(rel - 180.0) < 1e-9:
            return 0
        return 1 if rel > 180.0 else -1

    flips = 0
    prev = 0
    for h in headings:
        s = side(h)
        if s != 0 and prev != 0 and s != prev:
            flips += 1
        if s != 0:
            prev = s
    return flips


# ─── Maneuver consolidation ─────────────────────────────────────────────


def test_downwind_run_consolidates_gybes(polar_36_7):
    """~30 nm dead downwind (wind FROM north, finish due south).

    VMG-optimal sailing means gybing downwind — but a handful of long
    boards, not a gybe every 5-minute step. Pre-v13 this produced a
    staircase with side flips on most iterations.
    """
    wind = _uniform_wind(0.0, -6.0)  # 6 m/s from north
    result = compute_isochrone_route(
        start=(42.5, -88.0),
        finish=(42.0, -88.0),
        polar=polar_36_7,
        wind=wind,
        dt_minutes=5.0,
        max_iterations=200,
    )
    assert result.reached
    flips = _count_side_flips(result.headings, wind_from_deg=0.0)
    # Long boards: a 30 nm run needs at most a few gybes. Generous
    # ceiling — the pre-v13 staircase produced dozens.
    assert flips <= 6, f"route gybes {flips} times — staircase regression"


def test_upwind_beat_consolidates_tacks(polar_36_7):
    """~30 nm dead upwind. Must tack at least once, but not every step."""
    wind = _uniform_wind(0.0, 6.0)  # 6 m/s from south
    result = compute_isochrone_route(
        start=(42.5, -88.0),
        finish=(42.0, -88.0),  # finish due south = dead upwind
        polar=polar_36_7,
        wind=wind,
        dt_minutes=5.0,
        max_iterations=250,
    )
    flips = _count_side_flips(result.headings, wind_from_deg=180.0)
    assert flips >= 1, "an upwind beat with no tacks is geometrically impossible"
    assert flips <= 8, f"route tacks {flips} times — staircase regression"


def test_penalty_never_speeds_up_route(polar_36_7):
    """Adding maneuver cost cannot produce a faster route than no cost."""
    wind = _uniform_wind(0.0, 6.0)
    common = dict(
        start=(42.5, -88.0), finish=(42.0, -88.0),
        polar=polar_36_7, wind=wind,
        dt_minutes=5.0, max_iterations=250,
    )
    free = compute_isochrone_route(
        **common, tack_penalty_s=0.0, gybe_penalty_s=0.0,
    )
    penalized = compute_isochrone_route(**common)
    assert free.reached and penalized.reached
    assert penalized.total_minutes >= free.total_minutes


def test_zero_penalty_single_bin_matches_legacy_shape(polar_36_7):
    """Penalties 0 + frontier_per_bin 1 = pre-v13 configuration.

    Guards the compat knobs: the engine must still accept them and
    reach on the baseline downwind case from test_isochrone_engine.
    """
    wind = _uniform_wind(0.0, -5.0)
    result = compute_isochrone_route(
        start=(42.25, -88.0),
        finish=(42.0, -88.0),
        polar=polar_36_7,
        wind=wind,
        dt_minutes=5.0,
        max_iterations=100,
        tack_penalty_s=0.0,
        gybe_penalty_s=0.0,
        frontier_per_bin=1,
    )
    assert result.reached


# ─── Multi-leg pass-through ─────────────────────────────────────────────


def test_multileg_passes_penalties_through(polar_36_7):
    """The multileg driver forwards the v13 knobs to each leg."""
    wind = _uniform_wind(0.0, -6.0)
    marks = [
        {"lat": 42.5, "lon": -88.0},
        {"lat": 42.3, "lon": -88.0},
        {"lat": 42.0, "lon": -88.0},
    ]
    result = compute_isochrone_route_multileg(
        marks=marks, polar=polar_36_7, wind=wind,
        dt_minutes=5.0, max_iterations=250,
    )
    assert result.reached
    flips = _count_side_flips(result.headings, wind_from_deg=0.0)
    assert flips <= 8, f"multileg route gybes {flips} times — staircase regression"
