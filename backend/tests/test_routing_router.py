"""Tests for app/routers/routing.py.

Mocks asyncpg pool, redis client, and the isochrone engine. Verifies
ownership check, region resolution, cache hit/miss, and response shape.
No real wind data, no real engine call.
"""
from __future__ import annotations

import gzip
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, redis_client
from app.auth import get_current_user
from app.routers import routing
from app.services.routing.isochrone import IsochroneResult


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def fake_user():
    return {"uid": "test-uid", "email": "t@example.com", "tier": "free", "claims": {}}


@pytest.fixture
def mock_conn():
    return AsyncMock()


@pytest.fixture
def mock_redis(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(redis_client, "_client", mock)
    monkeypatch.setattr(redis_client, "_startup_error", None)
    return mock


@pytest.fixture
def app(fake_user, mock_conn):
    @asynccontextmanager
    async def fake_acquire():
        yield mock_conn

    pool = MagicMock()
    pool.acquire = fake_acquire

    app = FastAPI()
    app.include_router(routing.router)
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def waukegan_chicago_marks():
    return [
        {"name": "Start",  "lat": 42.3636, "lon": -87.8261},
        {"name": "Finish", "lat": 41.8881, "lon": -87.6132},
    ]


@pytest.fixture
def fake_wind_blob():
    """Gzipped JSON shaped like the worker's real output, covering Lake Michigan."""
    payload = {
        "source": "hrrr",
        "reference_time": "2026-05-05T12:00:00+00:00",
        "valid_time": "2026-05-05T13:00:00+00:00",
        "bbox": {"min_lat": 24.0, "max_lat": 50.0, "min_lon": -126.0, "max_lon": -66.0},
        "shape": [3, 3],
        "lats": [41.0, 42.0, 43.0],
        "lons": [-89.0, -88.0, -87.0],
        # Steady 5 m/s southerly across the grid (wind FROM south)
        "u": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        "v": [[5.0, 5.0, 5.0], [5.0, 5.0, 5.0], [5.0, 5.0, 5.0]],
    }
    return gzip.compress(json.dumps(payload).encode())


def _race_row(marks, boat_class="Beneteau First 36.7"):
    return {
        "id": uuid4(),
        "marks": json.dumps(marks),
        "boat_class": boat_class,
    }


def _fake_engine_result():
    return IsochroneResult(
        coords=[(42.3636, -87.8261), (42.1, -87.7), (41.8881, -87.6132)],
        total_minutes=420.0,
        tack_count=2,
        iterations=84,
        reached=True,
        nodes_explored=2400,
    )


# ─── Tests ───────────────────────────────────────────────────────────────


def test_compute_route_happy_path(
    client, mock_conn, mock_redis, waukegan_chicago_marks, fake_wind_blob,
):
    race = _race_row(waukegan_chicago_marks)
    mock_conn.fetchrow.return_value = race
    # 1st redis.get = wind blob, 2nd = route cache miss
    mock_redis.get.side_effect = [fake_wind_blob, None]
    mock_redis.setex.return_value = True

    with patch.object(
        routing, "compute_isochrone_route", return_value=_fake_engine_result(),
    ):
        r = client.post("/api/routing/compute", json={"race_id": str(race["id"])})

    assert r.status_code == 200
    body = r.json()
    assert body["route"]["type"] == "Feature"
    assert body["route"]["geometry"]["type"] == "LineString"
    assert len(body["route"]["geometry"]["coordinates"]) == 3
    # GeoJSON is (lon, lat)
    assert body["route"]["geometry"]["coordinates"][0] == [-87.8261, 42.3636]
    assert body["meta"]["reached"] is True
    assert body["meta"]["tack_count"] == 2
    assert body["meta"]["region"] == "conus"
    assert body["meta"]["cached"] is False
    assert body["meta"]["polar"] == "beneteau_36_7"
    mock_redis.setex.assert_awaited_once()


def test_compute_route_cache_hit(
    client, mock_conn, mock_redis, waukegan_chicago_marks, fake_wind_blob,
):
    race = _race_row(waukegan_chicago_marks)
    mock_conn.fetchrow.return_value = race

    cached_response = {
        "route": {"type": "Feature", "geometry": {"type": "LineString", "coordinates": []}, "properties": {}},
        "meta": {
            "total_minutes": 410.0, "tack_count": 1, "reached": True,
            "iterations": 80, "nodes_explored": 2000,
            "region": "conus", "wind_reference_time": "2026-05-05T12:00:00+00:00",
            "wind_valid_time": "2026-05-05T13:00:00+00:00",
            "polar": "beneteau_36_7", "cached": False,
        },
    }
    # 1st redis.get = wind blob, 2nd = cache hit
    mock_redis.get.side_effect = [fake_wind_blob, json.dumps(cached_response).encode()]

    with patch.object(routing, "compute_isochrone_route") as mock_engine:
        r = client.post("/api/routing/compute", json={"race_id": str(race["id"])})

    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["cached"] is True
    # Engine was never called on a cache hit
    mock_engine.assert_not_called()


def test_404_when_race_not_owned(client, mock_conn, mock_redis):
    mock_conn.fetchrow.return_value = None

    r = client.post("/api/routing/compute", json={"race_id": str(uuid4())})

    assert r.status_code == 404
    mock_redis.get.assert_not_awaited()


def test_400_when_too_few_marks(client, mock_conn, mock_redis):
    race = _race_row([{"name": "Solo", "lat": 42.0, "lon": -87.5}])
    mock_conn.fetchrow.return_value = race

    r = client.post("/api/routing/compute", json={"race_id": str(race["id"])})

    assert r.status_code == 400
    assert "at least 2 marks" in r.json()["detail"]


def test_503_when_no_wind_data(
    client, mock_conn, mock_redis, waukegan_chicago_marks,
):
    race = _race_row(waukegan_chicago_marks)
    mock_conn.fetchrow.return_value = race
    mock_redis.get.return_value = None  # redis miss on wind

    r = client.post("/api/routing/compute", json={"race_id": str(race["id"])})

    assert r.status_code == 503


def test_falls_back_to_default_polar_for_unknown_class(
    client, mock_conn, mock_redis, waukegan_chicago_marks, fake_wind_blob,
):
    race = _race_row(waukegan_chicago_marks, boat_class="J/105")
    mock_conn.fetchrow.return_value = race
    mock_redis.get.side_effect = [fake_wind_blob, None]
    mock_redis.setex.return_value = True

    with patch.object(
        routing, "compute_isochrone_route", return_value=_fake_engine_result(),
    ):
        r = client.post("/api/routing/compute", json={"race_id": str(race["id"])})

    assert r.status_code == 200
    # Still uses 36.7 polar since J/105 isn't transcribed yet
    assert r.json()["meta"]["polar"] == "beneteau_36_7"


def test_rejects_invalid_uuid(client, mock_conn):
    r = client.post("/api/routing/compute", json={"race_id": "not-a-uuid"})
    assert r.status_code == 422
    mock_conn.fetchrow.assert_not_awaited()
