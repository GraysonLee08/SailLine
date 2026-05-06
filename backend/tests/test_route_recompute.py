# backend/tests/test_route_recompute.py
"""Tests for the route_recompute background worker.

Covers the Google-Maps-style 'better route' notification logic:
    - Empty active-race set → no-op
    - First time seeing a race → silent baseline (no notification)
    - Improvement above threshold → publishes notification + updates baseline
    - Improvement below threshold → updates baseline only (no notification)
    - ForecastNotAvailable → skipped quietly
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import numpy as np
import pytest

from app.services.routing.isochrone import WindField, RouteResult
from app.services.routing.wind_forecast import WindForecast
from app.services.weather.forecast_loader import ForecastNotAvailable
from workers import route_recompute


# ─── Helpers ─────────────────────────────────────────────────────────────


def _race_row(total_minutes_was_seen: bool = True):
    return {
        "id": uuid4(),
        "user_id": "test-uid",
        "boat_class": "Beneteau First 36.7",
        "marks": json.dumps([
            {"name": "Waukegan", "lat": 42.3636, "lon": -87.8261},
            {"name": "Chicago",  "lat": 41.8881, "lon": -87.6132},
        ]),
        "start_at": datetime.now(timezone.utc) + timedelta(hours=2),
    }


def _fake_forecast():
    field = WindField(
        lats=np.array([41.0, 42.0, 43.0]),
        lons=np.array([-89.0, -88.0, -87.0]),
        u=np.zeros((3, 3), dtype=np.float32),
        v=np.full((3, 3), 5.0, dtype=np.float32),
        valid_time="2026-05-05T12:00:00+00:00",
        source="hrrr",
    )
    return WindForecast(snapshots=[field], quality="hrrr")


def _result(total_minutes: float) -> RouteResult:
    return RouteResult(
        path=[(42.3636, -87.8261), (41.8881, -87.6132)],
        headings=[200.0],
        total_minutes=total_minutes,
        tack_count=1, reached=True,
        iterations=50, nodes_explored=1000,
    )


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.setex.return_value = True
    redis.publish.return_value = 1
    return redis


# ─── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_active_races_is_noop(mock_pool, mock_redis):
    pool, conn = mock_pool
    conn.fetch.return_value = []

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_not_called()


@pytest.mark.asyncio
async def test_first_time_seeing_race_establishes_silent_baseline(
    mock_pool, mock_redis,
):
    """No prior route:last_best key → store new baseline, do not notify."""
    pool, conn = mock_pool
    race = _race_row()
    conn.fetch.return_value = [race]
    mock_redis.get.return_value = None  # no prior baseline

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.load_forecast_for_race",
               new=AsyncMock(return_value=_fake_forecast())), \
         patch("workers.route_recompute.compute_isochrone_route",
               return_value=_result(420.0)), \
         patch("workers.route_recompute.make_navigable_predicate",
               return_value=lambda *a, **k: True):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_not_called()
    # Baseline should have been written.
    setex_keys = [call.args[0] for call in mock_redis.setex.await_args_list]
    assert any(k.startswith("route:last_best:") for k in setex_keys)


@pytest.mark.asyncio
async def test_improvement_above_threshold_publishes_notification(
    mock_pool, mock_redis,
):
    """Last best 420 min, new 380 min → ~9.5% improvement → notify."""
    pool, conn = mock_pool
    race = _race_row()
    conn.fetch.return_value = [race]
    mock_redis.get.return_value = b"420.0"  # prior baseline

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.load_forecast_for_race",
               new=AsyncMock(return_value=_fake_forecast())), \
         patch("workers.route_recompute.compute_isochrone_route",
               return_value=_result(380.0)), \
         patch("workers.route_recompute.make_navigable_predicate",
               return_value=lambda *a, **k: True):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_awaited_once()
    channel, payload_blob = mock_redis.publish.await_args.args
    assert channel == f"route:notifications:{race['id']}"
    payload = json.loads(payload_blob)
    assert payload["old_total_minutes"] == 420.0
    assert payload["new_total_minutes"] == 380.0
    assert payload["improvement_pct"] > 5.0


@pytest.mark.asyncio
async def test_improvement_below_threshold_updates_baseline_only(
    mock_pool, mock_redis,
):
    """Last best 420, new 415 → ~1.2% improvement → no notification."""
    pool, conn = mock_pool
    race = _race_row()
    conn.fetch.return_value = [race]
    mock_redis.get.return_value = b"420.0"

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.load_forecast_for_race",
               new=AsyncMock(return_value=_fake_forecast())), \
         patch("workers.route_recompute.compute_isochrone_route",
               return_value=_result(415.0)), \
         patch("workers.route_recompute.make_navigable_predicate",
               return_value=lambda *a, **k: True):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_not_called()
    # Baseline still gets refreshed so slow drift can eventually accumulate.
    setex_keys = [call.args[0] for call in mock_redis.setex.await_args_list]
    assert any(k.startswith("route:last_best:") for k in setex_keys)


@pytest.mark.asyncio
async def test_forecast_not_available_skips_race_quietly(mock_pool, mock_redis):
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row()]

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.load_forecast_for_race",
               new=AsyncMock(side_effect=ForecastNotAvailable(
                   available_at=datetime.now(timezone.utc) + timedelta(hours=4),
               ))):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_not_called()
    mock_redis.setex.assert_not_called()


@pytest.mark.asyncio
async def test_engine_did_not_reach_finish_skips_notification(mock_pool, mock_redis):
    """Engine returned reached=False → don't pop a popup with a partial route."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row()]
    mock_redis.get.return_value = b"420.0"

    not_reached = RouteResult(
        path=[(42.3, -87.8)], headings=[],
        total_minutes=200.0, tack_count=0,
        reached=False, iterations=20, nodes_explored=100,
    )

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.load_forecast_for_race",
               new=AsyncMock(return_value=_fake_forecast())), \
         patch("workers.route_recompute.compute_isochrone_route",
               return_value=not_reached), \
         patch("workers.route_recompute.make_navigable_predicate",
               return_value=lambda *a, **k: True):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_not_called()