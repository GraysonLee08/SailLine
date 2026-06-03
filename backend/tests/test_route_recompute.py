# backend/tests/test_route_recompute.py
"""Tests for the route_recompute background worker.

Post Phase 2 the worker is a thin transport over
``app.services.routing.pipeline.compute_route``. These tests mock
``compute_route`` and cover the notification-logic concerns:

* Empty active-race set → no-op.
* First time seeing a race → silent baseline (no notification).
* Improvement above threshold → publishes notification + updates baseline.
* Improvement below threshold → updates baseline only (no notification).
* :class:`ForecastNotAvailable` from the pipeline → skipped quietly.
* Engine result with ``reached=False`` → no notification.
* Stored user knobs are replayed onto the RouteRequest the worker
  passes to ``compute_route`` (faithful-replay contract).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.routing.pipeline import (
    DeratingProfile,
    RouteOutcome,
    RouteRequestKnobs,
)
from app.services.weather.forecast_loader import ForecastNotAvailable
from workers import route_recompute


# ─── Helpers ─────────────────────────────────────────────────────────────


def _race_row():
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


def _outcome(total_minutes: float, reached: bool = True) -> RouteOutcome:
    return RouteOutcome(
        feature={"type": "Feature",
                 "geometry": {"type": "LineString", "coordinates": []},
                 "properties": {}},
        meta={
            "total_minutes": total_minutes,
            "tack_count": 1,
            "reached": reached,
            "iterations": 50,
            "nodes_explored": 1000,
            "legs": 1,
            "region": "conus",
        },
        cached=False,
        cache_key="route:v11-pipeline:test",
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
    mock_redis.get.return_value = None  # no prior baseline, no stored knobs

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.compute_route",
               new=AsyncMock(return_value=_outcome(420.0))):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_not_called()
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
    # Redis.get is called twice: once for last_request (None — cold start),
    # once for last_best (the baseline). Return baseline first by having
    # ``get`` short-circuit on the key string.
    async def fake_get(key):
        if "last_best" in str(key):
            return b"420.0"
        return None
    mock_redis.get.side_effect = fake_get

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.compute_route",
               new=AsyncMock(return_value=_outcome(380.0))):
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

    async def fake_get(key):
        if "last_best" in str(key):
            return b"420.0"
        return None
    mock_redis.get.side_effect = fake_get

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.compute_route",
               new=AsyncMock(return_value=_outcome(415.0))):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_not_called()
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
         patch("workers.route_recompute.compute_route",
               new=AsyncMock(side_effect=ForecastNotAvailable(
                   available_at=datetime.now(timezone.utc) + timedelta(hours=4),
               ))):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_not_called()
    # No baseline writes when the pipeline didn't produce a number.
    setex_keys = [call.args[0] for call in mock_redis.setex.await_args_list]
    assert not any(k.startswith("route:last_best:") for k in setex_keys)


@pytest.mark.asyncio
async def test_engine_did_not_reach_finish_skips_notification(
    mock_pool, mock_redis,
):
    """Pipeline returned reached=False → don't pop a popup with a partial route."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row()]

    async def fake_get(key):
        if "last_best" in str(key):
            return b"420.0"
        return None
    mock_redis.get.side_effect = fake_get

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.compute_route",
               new=AsyncMock(return_value=_outcome(200.0, reached=False))):
        await route_recompute.recompute_all()

    mock_redis.publish.assert_not_called()


@pytest.mark.asyncio
async def test_stored_knobs_are_replayed_onto_request(mock_pool, mock_redis):
    """Faithful-replay contract: the worker reads route:last_request,
    decodes it, and the RouteRequest handed to compute_route carries
    the same safety_factor / derating the user chose."""
    pool, conn = mock_pool
    race = _race_row()
    conn.fetch.return_value = [race]

    stored_knobs = RouteRequestKnobs(
        safety_factor=1.25,
        duration_hours=48.0,
        derating=DeratingProfile(
            max_tws_kt=30.0,
            polar_margin=0.95,
            hs_m=0.5,
            density_factor=1.02,
        ),
    )

    async def fake_get(key):
        if "last_request" in str(key):
            return stored_knobs.to_json()
        if "last_best" in str(key):
            return None
        return None
    mock_redis.get.side_effect = fake_get

    compute_mock = AsyncMock(return_value=_outcome(420.0))

    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=mock_redis), \
         patch("workers.route_recompute.compute_route", new=compute_mock):
        await route_recompute.recompute_all()

    compute_mock.assert_awaited_once()
    req = compute_mock.await_args.args[0]
    assert req.safety_factor == pytest.approx(1.25)
    assert req.duration_hours == pytest.approx(48.0)
    assert req.derating.max_tws_kt == pytest.approx(30.0)
    assert req.derating.polar_margin == pytest.approx(0.95)
    assert req.derating.hs_m == pytest.approx(0.5)
    assert req.derating.density_factor == pytest.approx(1.02)
    # Race-bound fields come from the DB row, not from the stored knobs.
    assert req.race_id == race["id"]
    assert req.boat_class == race["boat_class"]
    assert req.race_start == race["start_at"]
