# backend/tests/test_routing_router.py
"""Tests for /api/routing/compute under the rolling-forecast contract.

Mocks load_forecast_for_race rather than Redis blobs — the loader's own
test file (test_forecast_loader.py) covers the Redis-side details. This
keeps router tests focused on HTTP contract: 200 on success, 425 when
forecast pending, cache hit semantics, ownership scoping.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import routing as routing_module
from app.services.routing.isochrone import WindField, RouteResult
from app.services.routing.wind_forecast import WindForecast
from app.services.weather.forecast_loader import ForecastNotAvailable


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def waukegan_chicago_marks():
    return [
        {"name": "Waukegan", "lat": 42.3636, "lon": -87.8261},
        {"name": "Chicago",  "lat": 41.8881, "lon": -87.6132},
    ]


@pytest.fixture
def fake_forecast():
    """A 2-snapshot WindForecast covering 2 hours of southerly 5 m/s."""
    def _field(valid_iso):
        return WindField(
            lats=np.array([41.0, 42.0, 43.0]),
            lons=np.array([-89.0, -88.0, -87.0]),
            u=np.zeros((3, 3), dtype=np.float32),
            v=np.full((3, 3), 5.0, dtype=np.float32),
            reference_time="2026-05-05T12:00:00+00:00",
            valid_time=valid_iso,
            source="hrrr",
        )
    return WindForecast(
        snapshots=[
            _field("2026-05-05T12:00:00+00:00"),
            _field("2026-05-05T14:00:00+00:00"),
        ],
        quality="hrrr",
    )


@pytest.fixture
def fake_engine_result():
    return RouteResult(
        path=[(42.3636, -87.8261), (42.1, -87.7), (41.8881, -87.6132)],
        headings=[200.0, 200.0],
        total_minutes=420.0,
        tack_count=2,
        reached=True,
        iterations=84,
        nodes_explored=2400,
    )


@pytest.fixture
def race_row(waukegan_chicago_marks):
    return {
        "id": uuid4(),
        "marks": json.dumps(waukegan_chicago_marks),
        "boat_class": "Beneteau First 36.7",
        "start_at": datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc),
    }


@pytest.fixture
def mock_conn():
    conn = AsyncMock()
    return conn


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get.return_value = None  # no cache hits by default
    redis.setex.return_value = True
    return redis


@pytest.fixture
def client(mock_conn, mock_redis):
    """TestClient with auth, db, and redis dependency overrides."""
    from app import db, redis_client
    from app.auth import get_current_user

    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = mock_conn
    pool.acquire.return_value.__aexit__.return_value = None

    app.dependency_overrides[get_current_user] = lambda: {"uid": "test-uid"}
    app.dependency_overrides[db.get_pool] = lambda: pool

    with patch.object(redis_client, "get_client", return_value=mock_redis):
        yield TestClient(app)

    app.dependency_overrides.clear()


# ─── Tests ───────────────────────────────────────────────────────────────


def test_compute_route_happy_path(
    client, mock_conn, mock_redis, race_row, fake_forecast, fake_engine_result,
):
    mock_conn.fetchrow.return_value = race_row

    with patch.object(
        routing_module, "load_forecast_for_race",
        new=AsyncMock(return_value=fake_forecast),
    ), patch.object(
        routing_module, "compute_isochrone_route", return_value=fake_engine_result,
    ), patch.object(
        routing_module, "make_navigable_predicate", return_value=lambda *a, **k: True,
    ):
        r = client.post("/api/routing/compute",
                        json={"race_id": str(race_row["id"])})

    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["reached"] is True
    assert body["meta"]["forecast_quality"] == "hrrr"
    assert body["meta"]["cached"] is False
    assert body["meta"]["region"] == "conus"
    mock_redis.setex.assert_awaited_once()


def test_compute_route_returns_425_when_forecast_pending(
    client, mock_conn, race_row,
):
    mock_conn.fetchrow.return_value = race_row
    available_at = datetime.now(timezone.utc) + timedelta(hours=6)

    with patch.object(
        routing_module, "load_forecast_for_race",
        new=AsyncMock(side_effect=ForecastNotAvailable(
            available_at=available_at,
            reason="race starts beyond HRRR forecast horizon",
        )),
    ):
        r = client.post("/api/routing/compute",
                        json={"race_id": str(race_row["id"])})

    assert r.status_code == 425
    body = r.json()
    # FastAPI wraps detail dict under "detail"
    assert "available_at" in body["detail"]
    assert body["detail"]["hours_until_available"] > 0


def test_compute_route_cache_hit(
    client, mock_conn, mock_redis, race_row, fake_forecast,
):
    mock_conn.fetchrow.return_value = race_row
    cached_response = {
        "route": {"type": "Feature",
                  "geometry": {"type": "LineString", "coordinates": []},
                  "properties": {}},
        "meta": {
            "total_minutes": 410.0, "tack_count": 1, "reached": True,
            "iterations": 80, "nodes_explored": 2000,
            "region": "conus", "forecast_quality": "hrrr",
            "race_start": race_row["start_at"].isoformat(),
            "polar": "beneteau_36_7", "boat_class": "Beneteau First 36.7",
            "draft_m": 2.05, "min_depth_m": 3.075, "cached": False,
        },
    }
    mock_redis.get.return_value = json.dumps(cached_response).encode()

    with patch.object(
        routing_module, "load_forecast_for_race",
        new=AsyncMock(return_value=fake_forecast),
    ), patch.object(routing_module, "compute_isochrone_route") as mock_engine:
        r = client.post("/api/routing/compute",
                        json={"race_id": str(race_row["id"])})

    assert r.status_code == 200
    assert r.json()["meta"]["cached"] is True
    mock_engine.assert_not_called()  # cache hit short-circuits engine


def test_compute_route_404_when_race_not_owned(client, mock_conn):
    mock_conn.fetchrow.return_value = None
    r = client.post("/api/routing/compute",
                    json={"race_id": str(uuid4())})
    assert r.status_code == 404


def test_compute_route_400_when_fewer_than_two_marks(client, mock_conn):
    mock_conn.fetchrow.return_value = {
        "id": uuid4(),
        "marks": json.dumps([{"name": "only", "lat": 42.0, "lon": -88.0}]),
        "boat_class": "Beneteau First 36.7",
        "start_at": None,
    }
    r = client.post("/api/routing/compute", json={"race_id": str(uuid4())})
    assert r.status_code == 400
    assert "at least 2 marks" in r.json()["detail"]


def test_compute_route_falls_back_to_now_when_start_at_null(
    client, mock_conn, mock_redis, waukegan_chicago_marks,
    fake_forecast, fake_engine_result,
):
    """User computing a route on a race with no scheduled gun time."""
    mock_conn.fetchrow.return_value = {
        "id": uuid4(),
        "marks": json.dumps(waukegan_chicago_marks),
        "boat_class": "Beneteau First 36.7",
        "start_at": None,
    }
    with patch.object(
        routing_module, "load_forecast_for_race",
        new=AsyncMock(return_value=fake_forecast),
    ) as mock_loader, patch.object(
        routing_module, "compute_isochrone_route", return_value=fake_engine_result,
    ), patch.object(
        routing_module, "make_navigable_predicate", return_value=lambda *a, **k: True,
    ):
        r = client.post("/api/routing/compute", json={"race_id": str(uuid4())})

    assert r.status_code == 200
    # Loader was called with a race_start close to "now"
    called_with = mock_loader.await_args.kwargs
    delta = abs((called_with["race_start"] - datetime.now(timezone.utc)).total_seconds())
    assert delta < 5  # called within 5 seconds of now