# backend/tests/test_recorder_debrief.py
"""Tests for the recorder-debrief endpoint (Phase 2).

POST /api/races/{race_id}/recorder-debrief

Mocks asyncpg via FastAPI dependency overrides — same pattern as
``test_telemetry.py``. No real database.

Coverage:
  * auth — 403 unauthenticated, 404 cross-user, 201 happy path,
    uses race_write_predicate (not pre-D3 owner-only)
  * validation — schema_version, log entry caps, message length cap
  * persistence — single INSERT, correct args, JSONB payload shape
  * insert-only contract — repeated posts on the same race produce
    distinct rows (we don't UPDATE)
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user
from app.main import app
from app.routers.recorder_debrief import (
    MAX_LOG_MESSAGE_CHARS,
    MAX_RECENT_LOG_ENTRIES,
)


# ─── Constants & fixtures ────────────────────────────────────────────────


FAKE_UID = "test-user-uid"
FAKE_USER = {
    "uid": FAKE_UID,
    "email": "test@example.com",
    "tier": "free",
    "claims": {},
}


def _debrief_payload(
    schema_version: int = 1,
    points_captured: int = 1000,
    points_uploaded: int = 980,
    longest_success_gap_s: float = 27.4,
    recent_log: list[dict] | None = None,
) -> dict:
    """A complete, valid debrief blob. Tests override individual
    fields by passing kwargs."""
    return {
        "schema_version": schema_version,
        "device": {
            "platform": "android",
            "os_version": "14",
            "app_version": "0.1.0",
            "build_id": "72f7aaa8-4053-4bc2-8f83-0907c9702988",
        },
        "session": {
            "start_ts": "2026-05-31T15:10:00Z",
            "end_ts": "2026-05-31T16:50:00Z",
            "duration_s": 6000,
        },
        "capture": {
            "points_captured": points_captured,
            "points_uploaded": points_uploaded,
            "points_remaining_in_queue": points_captured - points_uploaded,
            "max_queue_depth": 120,
        },
        "uploads": {
            "attempts": 65,
            "successes": 60,
            "http_5xx": 1,
            "http_4xx": 0,
            "network_errors": 4,
            "longest_success_gap_s": longest_success_gap_s,
        },
        "recent_log": recent_log
        if recent_log is not None
        else [
            {
                "ts": "2026-05-31T15:11:12Z",
                "kind": "flush",
                "status": "ok",
                "http_status": 200,
                "duration_ms": 677.8,
                "batch_size": 30,
                "inserted": 30,
                "queue_depth_after": 0,
            }
        ],
    }


def _row(returned_id, created_at_iso="2026-06-01T16:00:00+00:00"):
    """Mimic asyncpg's row object — supports both dict-like and
    attribute access for the columns we read."""
    from datetime import datetime
    return {
        "id": returned_id,
        "created_at": datetime.fromisoformat(created_at_iso),
    }


@pytest.fixture
def fake_conn() -> MagicMock:
    """Mock asyncpg.Connection.

    Default state: the auth fetchrow returns truthy (race is writeable),
    the INSERT fetchrow returns a synthetic row with a known UUID.
    The endpoint calls fetchrow TWICE — once for the auth check, once
    for the INSERT RETURNING — so we use ``side_effect`` to dispatch
    by SQL.
    """
    conn = MagicMock()

    returned_id = uuid4()
    inserted_row = _row(returned_id)

    async def _fetchrow(sql: str, *args, **kwargs):
        if "INSERT INTO recorder_debriefs" in sql:
            return inserted_row
        if "FROM race_sessions" in sql:
            # Default: race is writeable. Override per-test by setting
            # ``fake_conn._auth_returns = None``.
            return getattr(conn, "_auth_returns", {"?column?": 1})
        return None

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    # Expose the planned id so tests can assert on it.
    conn._returned_id = returned_id
    return conn


@pytest.fixture
def fake_pool(fake_conn: MagicMock) -> MagicMock:
    pool = MagicMock()
    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = fake_conn
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


@pytest.fixture
def client(fake_pool: MagicMock):
    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    app.dependency_overrides[db.get_pool] = lambda: fake_pool
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client(fake_pool: MagicMock):
    app.dependency_overrides[db.get_pool] = lambda: fake_pool
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def race_url() -> str:
    return f"/api/races/{uuid4()}/recorder-debrief"


# ─── Auth ────────────────────────────────────────────────────────────────


def test_post_debrief_unauthenticated_403(unauth_client: TestClient):
    """No bearer → HTTPBearer auto-rejects with 403."""
    r = unauth_client.post(
        f"/api/races/{uuid4()}/recorder-debrief",
        json=_debrief_payload(),
    )
    assert r.status_code == 403


def test_post_debrief_cross_user_404(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    """Race not writeable → 404 (not 403, avoids leaking existence).

    The fixture's default returns a writeable race; we flip the
    auth-check return to None and assert the INSERT never fires.
    """
    fake_conn._auth_returns = None

    r = client.post(race_url, json=_debrief_payload())

    assert r.status_code == 404
    assert r.json()["detail"] == "race not found"
    # Only the auth fetchrow ran — no INSERT.
    assert fake_conn.fetchrow.await_count == 1


def test_post_debrief_uses_race_write_predicate(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    """Regression guard — auth check must hit ``race_write_predicate``
    (boat-crew aware). Without this, crew on shared boats lose the
    ability to debrief on co-owned races."""
    r = client.post(race_url, json=_debrief_payload())
    assert r.status_code == 201

    # Find the auth call (the SELECT against race_sessions).
    auth_sql = next(
        call.args[0]
        for call in fake_conn.fetchrow.await_args_list
        if "FROM race_sessions" in call.args[0]
    )
    assert "boat_crew" in auth_sql
    assert "bc.role IN ('owner', 'crew')" in auth_sql


# ─── Validation ──────────────────────────────────────────────────────────


def test_post_debrief_rejects_wrong_schema_version(
    client: TestClient, race_url: str
):
    """schema_version != 1 → 422 before the handler runs.

    Forward-compatibility lives at the field-add level; major version
    bumps require a new endpoint or explicit handling.
    """
    bad = _debrief_payload(schema_version=2)
    r = client.post(race_url, json=bad)
    assert r.status_code == 422


def test_post_debrief_rejects_too_many_log_entries(
    client: TestClient, race_url: str
):
    """recent_log capped at MAX_RECENT_LOG_ENTRIES on the wire so a
    runaway client can't bloat the DB."""
    entry = {
        "ts": "2026-05-31T15:11:12Z",
        "kind": "flush",
        "status": "ok",
        "http_status": 200,
    }
    bad = _debrief_payload(recent_log=[entry] * (MAX_RECENT_LOG_ENTRIES + 1))
    r = client.post(race_url, json=bad)
    assert r.status_code == 422


def test_post_debrief_rejects_oversized_log_message(
    client: TestClient, race_url: str
):
    """Per-entry message length is bounded."""
    bad_entry = {
        "ts": "2026-05-31T15:11:12Z",
        "kind": "error",
        "status": "error",
        "message": "x" * (MAX_LOG_MESSAGE_CHARS + 1),
    }
    bad = _debrief_payload(recent_log=[bad_entry])
    r = client.post(race_url, json=bad)
    assert r.status_code == 422


# ─── Happy path / persistence ────────────────────────────────────────────


def test_post_debrief_inserts_and_returns_row(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    """Successful POST returns 201 with the created row id."""
    payload = _debrief_payload()
    r = client.post(race_url, json=payload)

    assert r.status_code == 201
    body = r.json()
    assert body["id"] == str(fake_conn._returned_id)
    assert "created_at" in body

    # Two fetchrows: auth then INSERT.
    assert fake_conn.fetchrow.await_count == 2
    insert_sql, *insert_args = next(
        (call.args[0], *call.args[1:])
        for call in fake_conn.fetchrow.await_args_list
        if "INSERT INTO recorder_debriefs" in call.args[0]
    )
    assert "INSERT INTO recorder_debriefs" in insert_sql
    assert "session_id" in insert_sql
    assert "payload" in insert_sql
    assert "RETURNING id, created_at" in insert_sql

    # Args: race_id (UUID), payload (json-string).
    assert len(insert_args) == 2
    stored = json.loads(insert_args[1])
    assert stored["schema_version"] == 1
    assert stored["capture"]["points_captured"] == 1000
    assert stored["uploads"]["longest_success_gap_s"] == 27.4


def test_post_debrief_minimal_optional_fields_ok(
    client: TestClient, race_url: str
):
    """Optional device fields can be null. The mobile recorder may
    not always know the OS version or EAS build id (e.g. running in
    Expo Go before a custom dev client is installed)."""
    payload = _debrief_payload()
    payload["device"]["os_version"] = None
    payload["device"]["app_version"] = None
    payload["device"]["build_id"] = None
    payload["recent_log"] = []

    r = client.post(race_url, json=payload)
    assert r.status_code == 201


def test_post_debrief_each_post_inserts_new_row(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    """Insert-only contract — two posts on the same race produce two
    distinct INSERTs (not an UPDATE).

    History matters: a single race can have multiple recording
    sessions across stop / restart cycles, and each gets its own
    debrief row ordered by created_at.
    """
    r1 = client.post(race_url, json=_debrief_payload())
    r2 = client.post(race_url, json=_debrief_payload(points_captured=2000))

    assert r1.status_code == 201
    assert r2.status_code == 201

    insert_count = sum(
        1
        for call in fake_conn.fetchrow.await_args_list
        if "INSERT INTO recorder_debriefs" in call.args[0]
    )
    assert insert_count == 2

    # And no UPDATE statements anywhere.
    update_calls = [
        call
        for call in fake_conn.fetchrow.await_args_list
        if "UPDATE" in call.args[0].upper()
    ]
    assert update_calls == []
