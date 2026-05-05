"""Tests for app/routers/tracks.py.

Mocks the asyncpg pool by overriding the FastAPI `db.get_pool` dependency
and stubbing `get_current_user`. No real database is touched.

The ownership check (`_assert_race_owned`) issues a fetchrow before
every insert/select; tests configure its return value via the shared
mock_conn.fetchrow. Bulk insert is verified by inspecting the SQL the
router executes and the parallel-array arguments — we don't try to
actually run unnest in a fake.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user
from app.routers import tracks


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def fake_user():
    return {
        "uid": "test-uid",
        "email": "t@example.com",
        "tier": "free",
        "claims": {},
    }


@pytest.fixture
def mock_conn():
    return AsyncMock()


@pytest.fixture
def app(fake_user, mock_conn):
    @asynccontextmanager
    async def fake_acquire():
        yield mock_conn

    pool = MagicMock()
    pool.acquire = fake_acquire

    app = FastAPI()
    app.include_router(tracks.router)
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _race_owned(uid: str = "test-uid"):
    """Stand-in row for `SELECT 1 FROM race_sessions WHERE id=$1 AND user_id=$2`."""
    return {"?column?": 1}


def _sample_points(n: int = 3, start: datetime | None = None):
    start = start or datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    return [
        {
            "recorded_at": (start + timedelta(seconds=i)).isoformat(),
            "lat": 42.30 + i * 0.0001,
            "lon": -87.80 + i * 0.0001,
            "speed_kts": 5.5 + i * 0.1,
            "heading_deg": 180.0,
        }
        for i in range(n)
    ]


# ─── POST batch insert ───────────────────────────────────────────────────


def test_post_batch_inserts(client, mock_conn):
    mock_conn.fetchrow.return_value = _race_owned()
    race_id = uuid4()
    points = _sample_points(5)

    r = client.post(f"/api/races/{race_id}/track", json={"points": points})

    assert r.status_code == 201
    assert r.json() == {"inserted": 5}
    # ownership check + bulk insert = 2 round trips
    mock_conn.fetchrow.assert_awaited_once()
    mock_conn.execute.assert_awaited_once()
    args = mock_conn.execute.await_args.args
    sql = args[0]
    assert "INSERT INTO track_points" in sql
    assert "unnest" in sql.lower()
    assert "ST_SetSRID(ST_MakePoint" in sql
    # parallel arrays match input length
    assert args[1] == race_id
    for arr in args[2:]:
        assert len(arr) == 5
    # ownership check used the JWT uid, not anything from the body
    own_args = mock_conn.fetchrow.await_args.args
    assert own_args[1] == race_id
    assert own_args[2] == "test-uid"


def test_post_404_when_race_not_owned(client, mock_conn):
    mock_conn.fetchrow.return_value = None  # not yours, or doesn't exist

    r = client.post(
        f"/api/races/{uuid4()}/track",
        json={"points": _sample_points(2)},
    )

    assert r.status_code == 404
    mock_conn.execute.assert_not_awaited()


def test_post_rejects_empty_batch(client, mock_conn):
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": []})
    assert r.status_code == 422  # Pydantic min_length=1
    mock_conn.fetchrow.assert_not_awaited()


def test_post_rejects_oversized_batch(client, mock_conn):
    too_many = _sample_points(tracks.MAX_BATCH + 1)
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": too_many})
    assert r.status_code == 422
    mock_conn.fetchrow.assert_not_awaited()


def test_post_rejects_out_of_range_coords(client, mock_conn):
    bad = [
        {
            "recorded_at": "2026-05-09T14:00:00Z",
            "lat": 95.0,  # > 90
            "lon": -87.8,
        }
    ]
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": bad})
    assert r.status_code == 422


def test_post_accepts_missing_speed_and_heading(client, mock_conn):
    """Desktop browsers sometimes don't populate speed/heading — server
    should accept and store nulls."""
    mock_conn.fetchrow.return_value = _race_owned()
    points = [
        {
            "recorded_at": "2026-05-09T14:00:00Z",
            "lat": 42.30,
            "lon": -87.80,
        }
    ]

    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})

    assert r.status_code == 201
    assert r.json() == {"inserted": 1}
    args = mock_conn.execute.await_args.args
    speeds, headings = args[5], args[6]
    assert speeds == [None]
    assert headings == [None]


# ─── GET replay ──────────────────────────────────────────────────────────


def test_get_returns_chronological_track(client, mock_conn):
    race_id = uuid4()
    base = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    rows = [
        {
            "recorded_at": base + timedelta(seconds=i),
            "lat": 42.3 + i * 0.001,
            "lon": -87.8,
            "speed_kts": 5.5,
            "heading_deg": 180.0,
        }
        for i in range(3)
    ]
    mock_conn.fetchrow.return_value = _race_owned()
    mock_conn.fetch.return_value = rows

    r = client.get(f"/api/races/{race_id}/track")

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    assert body[0]["lat"] == 42.3
    assert body[2]["lat"] == 42.302

    sql = mock_conn.fetch.await_args.args[0]
    assert "ST_Y(position::geometry)" in sql
    assert "ST_X(position::geometry)" in sql
    assert "ORDER BY recorded_at ASC" in sql


def test_get_404_when_race_not_owned(client, mock_conn):
    mock_conn.fetchrow.return_value = None

    r = client.get(f"/api/races/{uuid4()}/track")

    assert r.status_code == 404
    mock_conn.fetch.assert_not_awaited()


def test_get_returns_empty_list_for_unrecorded_race(client, mock_conn):
    mock_conn.fetchrow.return_value = _race_owned()
    mock_conn.fetch.return_value = []

    r = client.get(f"/api/races/{uuid4()}/track")

    assert r.status_code == 200
    assert r.json() == []
