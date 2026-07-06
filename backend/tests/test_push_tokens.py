"""Tests for the device-push-token endpoints in app/routers/users.py.

Same fixture pattern as test_users_router.py: AsyncMock for asyncpg,
dependency overrides for auth + pool. Covers:

  * POST /me/push-tokens UPSERTs with the caller's uid
  * validation: short token, bad platform
  * DELETE /me/push-tokens scopes the delete to the caller
  * DELETE of an unknown token is still a 204 (idempotent)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user
from app.routers import users


VALID_TOKEN = "fcm-token-" + "x" * 32


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
    app.include_router(users.router, prefix="")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


@pytest.fixture
def client(user, mock_conn):
    return TestClient(_make_app(user, mock_conn))


# ─── POST /me/push-tokens ────────────────────────────────────────────


def test_register_upserts_with_caller_uid(client, mock_conn):
    r = client.post(
        "/users/me/push-tokens",
        json={"token": VALID_TOKEN, "platform": "android"},
    )
    assert r.status_code == 201
    assert r.json() == {"registered": True}
    sql = mock_conn.execute.await_args.args[0]
    args = mock_conn.execute.await_args.args[1:]
    assert "INSERT INTO device_push_tokens" in sql
    assert "ON CONFLICT (token) DO UPDATE" in sql
    # Token reassignment on conflict — the same device signing in as a
    # different account must move the token to the new user.
    assert "user_id = EXCLUDED.user_id" in sql
    assert args == (VALID_TOKEN, "u1", "android")


def test_register_rejects_short_token(client, mock_conn):
    r = client.post(
        "/users/me/push-tokens",
        json={"token": "tiny", "platform": "android"},
    )
    assert r.status_code == 422
    mock_conn.execute.assert_not_awaited()


def test_register_rejects_unknown_platform(client, mock_conn):
    r = client.post(
        "/users/me/push-tokens",
        json={"token": VALID_TOKEN, "platform": "blackberry"},
    )
    assert r.status_code == 422
    mock_conn.execute.assert_not_awaited()


# ─── DELETE /me/push-tokens ──────────────────────────────────────────


def test_delete_scopes_to_caller(client, mock_conn):
    r = client.delete(f"/users/me/push-tokens?token={VALID_TOKEN}")
    assert r.status_code == 204
    sql = mock_conn.execute.await_args.args[0]
    args = mock_conn.execute.await_args.args[1:]
    assert "DELETE FROM device_push_tokens" in sql
    assert "user_id = $2" in sql
    assert args == (VALID_TOKEN, "u1")


def test_delete_unknown_token_is_idempotent_204(client, mock_conn):
    # execute resolves fine whether or not a row matched — the endpoint
    # doesn't check rowcount, deliberately.
    r = client.delete("/users/me/push-tokens?token=never-registered-token")
    assert r.status_code == 204
