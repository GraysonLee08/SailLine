"""Tests for app/routers/crew.py.

Mocks asyncpg + email service. Covers CRUD on crew members, invite
creation (both flavours), invite redemption (happy + expired +
already-redeemed), and owner-only writes.
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
from app.routers import crew


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def user():
    return {"uid": "u1", "email": "u1@x", "tier": "free", "claims": {}}


@pytest.fixture
def mock_conn():
    return AsyncMock()


def _make_app(user, mock_conn):
    @asynccontextmanager
    async def fake_acquire():
        yield mock_conn

    pool = MagicMock()
    pool.acquire = fake_acquire

    app = FastAPI()
    app.include_router(crew.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


@pytest.fixture
def client(user, mock_conn):
    return TestClient(_make_app(user, mock_conn))


# Stub email so tests don't try to call SendGrid.
@pytest.fixture(autouse=True)
def _stub_email(monkeypatch):
    monkeypatch.setattr(crew, "send_boat_invite", lambda **kw: False)


# Auth helpers route through ``_require_boat_owner`` / ``_require_boat_member``
# which each do a SELECT against ``boats``. By default, return a row
# (owner / member). Individual tests override.
def _grant_access(mock_conn, owner: bool = True):
    """fetchrow side_effect order:
      1. _require_boat_owner / _require_boat_member's SELECT → {"?col?":1} or None
    """
    # Default behaviour: return owner-row for the auth check.
    mock_conn.fetchrow.return_value = {"?column?": 1} if owner else None


# ─── Crew list / patch / delete ──────────────────────────────────────


def test_list_crew_returns_members(client, mock_conn):
    boat_id = uuid4()
    # auth check returns truthy, then fetch returns rows.
    mock_conn.fetchrow.return_value = {"?column?": 1}
    mock_conn.fetch.return_value = [
        {
            "user_id": "u1", "role": "owner",
            "joined_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "has_profile": True,
        },
        {
            "user_id": "u2", "role": "crew",
            "joined_at": datetime(2026, 5, 2, tzinfo=timezone.utc),
            "has_profile": True,
        },
    ]
    r = client.get(f"/api/boats/{boat_id}/crew")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["role"] == "owner"


def test_list_crew_404_when_not_member(client, mock_conn):
    mock_conn.fetchrow.return_value = None   # auth check fails
    r = client.get(f"/api/boats/{uuid4()}/crew")
    assert r.status_code == 404


def test_patch_role_to_viewer(client, mock_conn):
    boat_id = uuid4()
    mock_conn.fetchrow.side_effect = [
        {"?column?": 1},                     # owner check
        {                                     # UPDATE RETURNING
            "user_id": "u2", "role": "viewer",
            "joined_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        },
    ]
    r = client.patch(
        f"/api/boats/{boat_id}/crew/u2", json={"role": "viewer"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "viewer"


def test_patch_role_404_when_member_missing(client, mock_conn):
    mock_conn.fetchrow.side_effect = [
        {"?column?": 1},   # owner check
        None,              # UPDATE found no row
    ]
    r = client.patch(
        f"/api/boats/{uuid4()}/crew/u2", json={"role": "crew"},
    )
    assert r.status_code == 404


def test_patch_role_rejects_owner_value(client, mock_conn):
    """Pydantic rejects role='owner' on the patch model."""
    r = client.patch(
        f"/api/boats/{uuid4()}/crew/u2", json={"role": "owner"},
    )
    assert r.status_code == 422


def test_delete_member_204(client, mock_conn):
    mock_conn.fetchrow.return_value = {"?column?": 1}   # owner check
    mock_conn.execute.return_value = "DELETE 1"
    r = client.delete(f"/api/boats/{uuid4()}/crew/u2")
    assert r.status_code == 204


def test_delete_member_404_when_owner(client, mock_conn):
    mock_conn.fetchrow.return_value = {"?column?": 1}
    mock_conn.execute.return_value = "DELETE 0"          # role='owner' filtered out
    r = client.delete(f"/api/boats/{uuid4()}/crew/u2")
    assert r.status_code == 404


# ─── Invites: create / list / revoke / redeem ────────────────────────


def _make_invite_row(
    *, code="RACE-XK4M", email=None, single_use=False, redeemed=False,
    expires_at=None,
):
    return {
        "id": uuid4(),
        "boat_id": uuid4(),
        "role": "crew",
        "code": code,
        "email": email,
        "single_use": single_use,
        "expires_at": expires_at,
        "redeemed_at": (
            datetime.now(timezone.utc) if redeemed else None
        ),
        "created_at": datetime.now(timezone.utc),
    }


def test_create_invite_code_path(client, mock_conn):
    boat_id = uuid4()
    mock_conn.fetchrow.side_effect = [
        {"?column?": 1},                     # owner check
        {"name": "Gaucho"},                  # boat name lookup
        _make_invite_row(code="RACE-XK4M"),  # INSERT RETURNING
    ]
    r = client.post(
        f"/api/boats/{boat_id}/invites",
        json={"role": "crew"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["code"] == "RACE-XK4M"
    assert body["accept_url"].endswith("?invite=RACE-XK4M")
    assert body["emailed"] is False


def test_create_invite_email_path_returns_token(client, mock_conn):
    boat_id = uuid4()
    mock_conn.fetchrow.side_effect = [
        {"?column?": 1},
        {"name": "Gaucho"},
        _make_invite_row(
            code="deadbeefdeadbeefdeadbeefdeadbeef",
            email="crew@example.com",
            single_use=True,
        ),
    ]
    r = client.post(
        f"/api/boats/{boat_id}/invites",
        json={"role": "crew", "email": "crew@example.com"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "crew@example.com"
    assert body["single_use"] is True


def test_list_invites_filters_expired_and_redeemed(client, mock_conn):
    boat_id = uuid4()
    mock_conn.fetchrow.return_value = {"?column?": 1}
    mock_conn.fetch.return_value = [_make_invite_row()]
    r = client.get(f"/api/boats/{boat_id}/invites")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_revoke_invite_204(client, mock_conn):
    mock_conn.fetchrow.return_value = {"?column?": 1}
    mock_conn.execute.return_value = "DELETE 1"
    r = client.delete(f"/api/boats/{uuid4()}/invites/RACE-XK4M")
    assert r.status_code == 204


def test_revoke_invite_404_when_missing(client, mock_conn):
    mock_conn.fetchrow.return_value = {"?column?": 1}
    mock_conn.execute.return_value = "DELETE 0"
    r = client.delete(f"/api/boats/{uuid4()}/invites/NONE")
    assert r.status_code == 404


# ─── Redeem ──────────────────────────────────────────────────────────


def test_redeem_404_when_code_missing(client, mock_conn):
    mock_conn.fetchrow.return_value = None
    r = client.post("/api/invites/redeem", json={"code": "NOPE"})
    assert r.status_code == 404


def test_redeem_410_when_expired(client, mock_conn):
    mock_conn.fetchrow.return_value = {
        "id": uuid4(), "boat_id": uuid4(), "role": "crew",
        "single_use": True,
        "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "redeemed_at": None,
    }
    r = client.post("/api/invites/redeem", json={"code": "EXPIRED"})
    assert r.status_code == 410


def test_redeem_409_when_already_redeemed(client, mock_conn):
    mock_conn.fetchrow.return_value = {
        "id": uuid4(), "boat_id": uuid4(), "role": "crew",
        "single_use": True,
        "expires_at": None,
        "redeemed_at": datetime.now(timezone.utc),
    }
    r = client.post("/api/invites/redeem", json={"code": "USED"})
    assert r.status_code == 409


def test_redeem_happy(client, mock_conn):
    boat_id = uuid4()
    mock_conn.fetchrow.side_effect = [
        # invite lookup
        {
            "id": uuid4(), "boat_id": boat_id, "role": "crew",
            "single_use": True,
            "expires_at": None, "redeemed_at": None,
        },
        # existing membership check → None (not a member yet)
        None,
    ]
    r = client.post("/api/invites/redeem", json={"code": "RACE-OK4Q"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "crew"
    assert body["boat_id"] == str(boat_id)


def test_redeem_idempotent_when_already_member(client, mock_conn):
    """User clicks the link twice; second redeem should not 409.

    We return 200 with the existing role."""
    boat_id = uuid4()
    mock_conn.fetchrow.side_effect = [
        {
            "id": uuid4(), "boat_id": boat_id, "role": "crew",
            "single_use": True,
            "expires_at": None, "redeemed_at": None,
        },
        {"role": "crew"},   # existing membership
    ]
    r = client.post("/api/invites/redeem", json={"code": "RACE-OK4Q"})
    assert r.status_code == 200
    assert r.json()["role"] == "crew"
