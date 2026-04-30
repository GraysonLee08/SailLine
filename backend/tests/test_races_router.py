"""Tests for app/routers/races.py.

Mocks the asyncpg pool by overriding the FastAPI `db.get_pool` dependency
and stubbing `get_current_user`. No real database is touched.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user
from app.routers import races


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
    """The asyncpg Connection mock. Tests configure return values on this."""
    return AsyncMock()


@pytest.fixture
def app(fake_user, mock_conn):
    """Fresh FastAPI app wired up with the races router and dependency overrides."""
    @asynccontextmanager
    async def fake_acquire():
        yield mock_conn

    pool = MagicMock()
    pool.acquire = fake_acquire

    app = FastAPI()
    app.include_router(races.router)
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _make_row(**overrides):
    """Build a fake DB row dict matching the SELECT projection."""
    now = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    base = {
        "id": uuid4(),
        "name": "Saturday Buoy Race",
        "mode": "inshore",
        "boat_class": "J/105",
        "marks": json.dumps([
            {"name": "Start", "lat": 41.9, "lon": -87.6},
            {"name": "M1", "lat": 42.0, "lon": -87.5},
        ]),
        "started_at": None,
        "ended_at": None,
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return base


# ─── List ────────────────────────────────────────────────────────────────

def test_list_empty(client, mock_conn):
    mock_conn.fetch.return_value = []

    r = client.get("/api/races")

    assert r.status_code == 200
    assert r.json() == []


def test_list_returns_rows(client, mock_conn):
    row = _make_row()
    mock_conn.fetch.return_value = [row]

    r = client.get("/api/races")

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "Saturday Buoy Race"
    assert body[0]["marks"][0] == {"name": "Start", "lat": 41.9, "lon": -87.6}


# ─── Create ──────────────────────────────────────────────────────────────

def test_create_race(client, mock_conn):
    mock_conn.fetchrow.return_value = _make_row()

    payload = {
        "name": "Saturday Buoy Race",
        "mode": "inshore",
        "boat_class": "J/105",
        "marks": [
            {"name": "Start", "lat": 41.9, "lon": -87.6},
            {"name": "M1", "lat": 42.0, "lon": -87.5},
        ],
    }
    r = client.post("/api/races", json=payload)

    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Saturday Buoy Race"
    assert len(body["marks"]) == 2
    mock_conn.fetchrow.assert_awaited_once()


def test_create_rejects_invalid_mode(client, mock_conn):
    payload = {
        "name": "X",
        "mode": "bogus",  # not in {inshore, distance}
        "boat_class": "J/105",
        "marks": [],
    }
    r = client.post("/api/races", json=payload)

    assert r.status_code == 422
    mock_conn.fetchrow.assert_not_awaited()


def test_create_rejects_out_of_range_lat(client, mock_conn):
    payload = {
        "name": "X",
        "mode": "inshore",
        "boat_class": "J/105",
        "marks": [{"name": "M", "lat": 91.0, "lon": 0.0}],
    }
    r = client.post("/api/races", json=payload)

    assert r.status_code == 422
    mock_conn.fetchrow.assert_not_awaited()


def test_create_with_empty_marks_is_allowed(client, mock_conn):
    """User can create the shell of a race and add marks later."""
    mock_conn.fetchrow.return_value = _make_row(marks="[]")

    payload = {
        "name": "Draft",
        "mode": "distance",
        "boat_class": "Beneteau First 36.7",
        "marks": [],
    }
    r = client.post("/api/races", json=payload)

    assert r.status_code == 201
    assert r.json()["marks"] == []


# ─── Get by id ───────────────────────────────────────────────────────────

def test_get_by_id_found(client, mock_conn):
    row = _make_row()
    mock_conn.fetchrow.return_value = row

    r = client.get(f"/api/races/{row['id']}")

    assert r.status_code == 200
    assert r.json()["id"] == str(row["id"])


def test_get_by_id_404(client, mock_conn):
    mock_conn.fetchrow.return_value = None

    r = client.get(f"/api/races/{uuid4()}")

    assert r.status_code == 404


# ─── Update ──────────────────────────────────────────────────────────────

def test_patch_rename(client, mock_conn):
    row = _make_row(name="Renamed")
    mock_conn.fetchrow.return_value = row

    r = client.patch(f"/api/races/{row['id']}", json={"name": "Renamed"})

    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"


def test_patch_replaces_marks(client, mock_conn):
    new_marks = [{"name": "S", "lat": 1.0, "lon": 2.0}]
    row = _make_row(marks=json.dumps(new_marks))
    mock_conn.fetchrow.return_value = row

    r = client.patch(f"/api/races/{row['id']}", json={"marks": new_marks})

    assert r.status_code == 200
    assert r.json()["marks"] == new_marks


def test_patch_empty_body_400(client, mock_conn):
    r = client.patch(f"/api/races/{uuid4()}", json={})

    assert r.status_code == 400
    mock_conn.fetchrow.assert_not_awaited()


def test_patch_404(client, mock_conn):
    mock_conn.fetchrow.return_value = None

    r = client.patch(f"/api/races/{uuid4()}", json={"name": "x"})

    assert r.status_code == 404


# ─── Delete ──────────────────────────────────────────────────────────────

def test_delete_success(client, mock_conn):
    mock_conn.execute.return_value = "DELETE 1"

    r = client.delete(f"/api/races/{uuid4()}")

    assert r.status_code == 204


def test_delete_404(client, mock_conn):
    mock_conn.execute.return_value = "DELETE 0"

    r = client.delete(f"/api/races/{uuid4()}")

    assert r.status_code == 404
