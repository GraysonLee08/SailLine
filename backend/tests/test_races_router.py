"""Tests for app/routers/races.py.

Strategy:
- Use TestClient with dependency_overrides to swap get_current_user (no real
  Firebase token) and get_pool (a FakePool that records SQL + canned rows).
- No real DB. Tests are about: routing, auth wiring, ownership-boundary 404s,
  payload validation pass-through, and that the SQL we *would* run uses the
  authenticated user's uid (not anything from the request body).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.db import get_pool
from app.main import app


# ---------------------------------------------------------------------------
# Test doubles


class FakeConn:
    """Stand-in for an asyncpg connection. Records the last call for asserts."""

    def __init__(self, fetchrow_return=None, fetch_return=None, fetchval_return=None):
        self.fetchrow_return = fetchrow_return
        self.fetch_return = fetch_return or []
        self.fetchval_return = fetchval_return
        self.calls: list[tuple[str, str, tuple]] = []  # (method, sql, args)

    async def fetchrow(self, sql: str, *args: Any):
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrow_return

    async def fetch(self, sql: str, *args: Any):
        self.calls.append(("fetch", sql, args))
        return self.fetch_return

    async def fetchval(self, sql: str, *args: Any):
        self.calls.append(("fetchval", sql, args))
        return self.fetchval_return


class FakePool:
    """Pool that yields a FakeConn from acquire()."""

    def __init__(self, conn: FakeConn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


# ---------------------------------------------------------------------------
# Fixtures


TEST_UID = "firebase-uid-abc123"
OTHER_UID = "firebase-uid-someone-else"


def _course_payload() -> dict:
    return {
        "marks": [
            {"id": "S", "name": "Start/Finish", "lat": 41.920, "lon": -87.610},
            {"id": "W", "name": "Windward",     "lat": 41.955, "lon": -87.605},
            {"id": "L", "name": "Leeward",      "lat": 41.890, "lon": -87.615},
        ],
        "course": [
            {"mark_id": "S"},
            {"mark_id": "W", "rounding": "port"},
            {"mark_id": "L", "rounding": "port"},
            {"mark_id": "S"},
        ],
        "laps": 3,
    }


def _create_payload() -> dict:
    return {
        "name": "Saturday MORF Race 14",
        "mode": "inshore",
        "boat_class": "J/105",
        "course": _course_payload(),
    }


def _row(uid: str = TEST_UID, race_id: UUID | None = None) -> dict:
    """A complete row matching the SELECT columns in races.py."""
    return {
        "id": race_id or uuid4(),
        "user_id": uid,
        "name": "Saturday MORF Race 14",
        "mode": "inshore",
        "boat_class": "J/105",
        "course": _course_payload(),
        "started_at": None,
        "ended_at": None,
        "created_at": datetime(2026, 4, 30, 14, 0, tzinfo=timezone.utc),
    }


@pytest.fixture
def fake_user() -> dict:
    """Stand-in for what get_current_user produces."""
    return {
        "uid": TEST_UID,
        "email": "skipper@example.com",
        "tier": "free",
        "claims": {"email_verified": True},
    }


@pytest.fixture
def client(fake_user: dict):
    """TestClient with auth + pool dependencies overridable per-test.

    Each test sets `app.dependency_overrides[get_pool]` with the FakePool
    it needs. Auth is overridden once for the whole fixture to fake_user.
    """
    app.dependency_overrides[get_current_user] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


def _use_pool(conn: FakeConn) -> FakePool:
    """Wire a FakePool up as the get_pool override and return it."""
    pool = FakePool(conn)
    app.dependency_overrides[get_pool] = lambda: pool
    return pool


# ---------------------------------------------------------------------------
# POST /api/races


def test_create_race_returns_201_and_body(client: TestClient):
    conn = FakeConn(fetchrow_return=_row())
    _use_pool(conn)

    resp = client.post("/api/races", json=_create_payload())

    assert resp.status_code == 201
    body = resp.json()
    assert body["user_id"] == TEST_UID
    assert body["mode"] == "inshore"
    assert body["course"]["laps"] == 3


def test_create_race_uses_authenticated_uid_not_request_body(client: TestClient):
    """Even if the client sends a user_id, we ignore it and use the JWT uid."""
    conn = FakeConn(fetchrow_return=_row())
    _use_pool(conn)
    payload = _create_payload() | {"user_id": "attacker-controlled"}

    resp = client.post("/api/races", json=payload)

    # Pydantic strict mode rejects the extra field outright — even better than
    # silently ignoring it.
    assert resp.status_code == 422

    # And on the legitimate request, the uid passed to SQL is the JWT one.
    conn2 = FakeConn(fetchrow_return=_row())
    _use_pool(conn2)
    client.post("/api/races", json=_create_payload())
    method, _sql, args = conn2.calls[0]
    assert method == "fetchrow"
    assert args[0] == TEST_UID  # first $1 in the INSERT


def test_create_race_rejects_invalid_course(client: TestClient):
    """Pydantic validation runs before we touch the DB."""
    _use_pool(FakeConn())  # shouldn't be called
    bad = _create_payload()
    bad["course"]["course"][1]["mark_id"] = "ZZZ"  # not in marks

    resp = client.post("/api/races", json=bad)

    assert resp.status_code == 422


def test_create_race_rejects_unknown_boat_class(client: TestClient):
    _use_pool(FakeConn())
    bad = _create_payload() | {"boat_class": "Optimist"}

    resp = client.post("/api/races", json=bad)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/races


def test_list_races_returns_only_current_users_rows(client: TestClient):
    rows = [_row(), _row()]
    conn = FakeConn(fetch_return=rows)
    _use_pool(conn)

    resp = client.get("/api/races")

    assert resp.status_code == 200
    assert len(resp.json()) == 2

    method, sql, args = conn.calls[0]
    assert method == "fetch"
    assert "WHERE user_id = $1" in sql
    assert args[0] == TEST_UID


def test_list_races_empty(client: TestClient):
    _use_pool(FakeConn(fetch_return=[]))

    resp = client.get("/api/races")

    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/races/{id}


def test_get_race_found(client: TestClient):
    rid = uuid4()
    conn = FakeConn(fetchrow_return=_row(race_id=rid))
    _use_pool(conn)

    resp = client.get(f"/api/races/{rid}")

    assert resp.status_code == 200
    assert resp.json()["id"] == str(rid)


def test_get_race_404_when_not_found(client: TestClient):
    _use_pool(FakeConn(fetchrow_return=None))

    resp = client.get(f"/api/races/{uuid4()}")

    assert resp.status_code == 404


def test_get_race_404_when_owned_by_someone_else(client: TestClient):
    """Ownership-boundary check is in the SQL (WHERE user_id = $2).

    This test confirms the SQL includes that filter — so a row owned by
    another uid would not be returned, which surfaces as 404."""
    conn = FakeConn(fetchrow_return=None)  # query returns nothing for our uid
    _use_pool(conn)

    resp = client.get(f"/api/races/{uuid4()}")

    assert resp.status_code == 404
    _method, sql, args = conn.calls[0]
    assert "WHERE id = $1 AND user_id = $2" in sql
    assert args[1] == TEST_UID


def test_get_race_rejects_non_uuid_path(client: TestClient):
    _use_pool(FakeConn())
    resp = client.get("/api/races/not-a-uuid")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/races/{id}


def test_delete_race_204(client: TestClient):
    rid = uuid4()
    conn = FakeConn(fetchval_return=rid)
    _use_pool(conn)

    resp = client.delete(f"/api/races/{rid}")

    assert resp.status_code == 204
    _method, sql, args = conn.calls[0]
    assert "DELETE FROM race_sessions" in sql
    assert args[1] == TEST_UID


def test_delete_race_404_when_not_found(client: TestClient):
    _use_pool(FakeConn(fetchval_return=None))

    resp = client.delete(f"/api/races/{uuid4()}")

    assert resp.status_code == 404
