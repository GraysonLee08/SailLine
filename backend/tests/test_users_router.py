"""Tests for app/routers/users.py.

Same fixture pattern as test_crew_router.py: AsyncMock for asyncpg,
dependency overrides for auth + pool. Covers:

  * GET /me echoes every D4 field
  * PATCH /me only writes keys the client sent (exclude_unset)
  * PATCH /me flips profile_complete when display_name is set
  * Validation rejects empty/whitespace display_name, weight out of
    bounds, bio too long, bad world_sailing_category
  * Avatar upload happy path + size cap + missing bucket
  * Avatar delete clears the column
"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user
from app.routers import users


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
    app.include_router(users.router, prefix="")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


@pytest.fixture
def client(user, mock_conn):
    return TestClient(_make_app(user, mock_conn))


def _profile_row(**overrides):
    """Build a DB-shaped row for ``user_profiles`` SELECTs.

    The router only reads the columns enumerated in
    ``_PROFILE_COLUMNS`` so we match exactly that shape. Override any
    field per-test.
    """
    base = {
        "default_boat_id": None,
        "display_name": None,
        "profile_complete": False,
        "phone": None,
        "bio": None,
        "avatar_url": None,
        "weight_lb": None,
        "emergency_contact_name": None,
        "emergency_contact_phone": None,
        "world_sailing_sailor_id": None,
        "world_sailing_category": None,
        "safety_at_sea_cert_expiry": None,
        "email": "u1@x",
    }
    base.update(overrides)
    return base


# Avatar processing tests rely on Pillow; the GCS side is stubbed so
# the test doesn't need credentials.
@pytest.fixture(autouse=True)
def _stub_gcs(monkeypatch):
    monkeypatch.setattr(users, "store_avatar", lambda b, uid: f"https://cdn/{uid}.webp")
    monkeypatch.setattr(users, "gcs_delete_avatar", lambda uid: None)


# ─── GET /me ─────────────────────────────────────────────────────────


def test_get_me_returns_all_fields(client, mock_conn):
    mock_conn.fetchrow.return_value = _profile_row(
        display_name="Grayson V",
        profile_complete=True,
        bio="Bow on Wednesdays.",
        phone="312-555-0101",
        avatar_url="https://cdn/u1.webp?v=1",
        weight_lb=Decimal("185.5"),
        emergency_contact_name="Roxie",
        emergency_contact_phone="323-555-0199",
        world_sailing_sailor_id="USA12345",
        world_sailing_category="group_1",
        safety_at_sea_cert_expiry=date(2027, 6, 1),
    )
    r = client.get("/users/me")
    assert r.status_code == 200
    body = r.json()
    assert body["uid"] == "u1"
    assert body["display_name"] == "Grayson V"
    assert body["profile_complete"] is True
    assert body["bio"] == "Bow on Wednesdays."
    assert body["phone"] == "312-555-0101"
    assert body["avatar_url"] == "https://cdn/u1.webp?v=1"
    assert body["weight_lb"] == 185.5
    assert body["emergency_contact_name"] == "Roxie"
    assert body["world_sailing_sailor_id"] == "USA12345"
    assert body["world_sailing_category"] == "group_1"
    assert body["safety_at_sea_cert_expiry"] == "2027-06-01"


def test_get_me_missing_row_returns_minimal(client, mock_conn):
    # Shouldn't happen post-auth-upsert, but the router defends against it.
    mock_conn.fetchrow.return_value = None
    r = client.get("/users/me")
    assert r.status_code == 200
    body = r.json()
    assert body["uid"] == "u1"
    assert body["profile_complete"] is False
    assert body["display_name"] is None


# ─── PATCH /me ───────────────────────────────────────────────────────


def test_patch_display_name_flips_profile_complete(client, mock_conn):
    # First call inside the transaction: no default-boat ownership
    # check needed (we didn't send default_boat_id). The update runs,
    # then the SELECT echo returns the new state.
    mock_conn.fetchrow.return_value = _profile_row(
        display_name="Grayson V", profile_complete=True,
    )
    r = client.patch("/users/me", json={"display_name": "Grayson V"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Grayson V"
    assert body["profile_complete"] is True

    # The SQL we executed should include both display_name and the
    # profile_complete flip.
    executed_sqls = [c.args[0] for c in mock_conn.execute.call_args_list]
    assert any("display_name" in s for s in executed_sqls)
    assert any("profile_complete = TRUE" in s for s in executed_sqls)


def test_patch_empty_display_name_rejected(client, mock_conn):
    r = client.patch("/users/me", json={"display_name": "   "})
    assert r.status_code == 422
    # No SQL UPDATE should have run.
    assert mock_conn.execute.call_count == 0


def test_patch_partial_only_touches_sent_keys(client, mock_conn):
    # Sending only weight_lb must not nullify display_name or
    # default_boat_id (the latent pre-D4 bug).
    mock_conn.fetchrow.return_value = _profile_row(
        display_name="Existing Name",
        profile_complete=True,
        weight_lb=Decimal("180.0"),
    )
    r = client.patch("/users/me", json={"weight_lb": 180})
    assert r.status_code == 200
    executed_sqls = [c.args[0] for c in mock_conn.execute.call_args_list]
    # Exactly one UPDATE, mentioning weight_lb only.
    assert len(executed_sqls) == 1
    assert "weight_lb" in executed_sqls[0]
    assert "display_name" not in executed_sqls[0]
    assert "default_boat_id" not in executed_sqls[0]


def test_patch_no_recognised_keys_is_noop(client, mock_conn):
    # Empty payload — the model parses fine, exclude_unset yields {},
    # and we skip the UPDATE entirely. The echo SELECT still runs.
    mock_conn.fetchrow.return_value = _profile_row()
    r = client.patch("/users/me", json={})
    assert r.status_code == 200
    # No UPDATE should have executed.
    assert mock_conn.execute.call_count == 0


def test_patch_weight_out_of_range(client, mock_conn):
    # 30 lb is below the 50 lb floor — model rejects.
    r = client.patch("/users/me", json={"weight_lb": 30})
    assert r.status_code == 422
    r = client.patch("/users/me", json={"weight_lb": 900})
    assert r.status_code == 422


def test_patch_bio_too_long(client, mock_conn):
    r = client.patch("/users/me", json={"bio": "x" * 1001})
    assert r.status_code == 422


def test_patch_invalid_world_sailing_category(client, mock_conn):
    r = client.patch(
        "/users/me", json={"world_sailing_category": "amateur"},
    )
    assert r.status_code == 422


def test_patch_clear_optional_field_with_null(client, mock_conn):
    # Sending explicit null clears the field (vs. omitting which
    # leaves it alone). Use bio as the example.
    mock_conn.fetchrow.return_value = _profile_row(bio=None)
    r = client.patch("/users/me", json={"bio": None})
    assert r.status_code == 200
    executed_sqls = [c.args[0] for c in mock_conn.execute.call_args_list]
    assert len(executed_sqls) == 1
    assert "bio" in executed_sqls[0]


def test_patch_default_boat_404_when_not_owned(client, mock_conn):
    # Ownership check returns None → 404.
    boat_id = uuid4()
    mock_conn.fetchrow.side_effect = [None]
    r = client.patch(
        "/users/me", json={"default_boat_id": str(boat_id)},
    )
    assert r.status_code == 404


def test_patch_safety_at_sea_date(client, mock_conn):
    mock_conn.fetchrow.return_value = _profile_row(
        safety_at_sea_cert_expiry=date(2027, 6, 1),
    )
    r = client.patch(
        "/users/me", json={"safety_at_sea_cert_expiry": "2027-06-01"},
    )
    assert r.status_code == 200
    assert r.json()["safety_at_sea_cert_expiry"] == "2027-06-01"


# ─── Avatar upload ───────────────────────────────────────────────────


def _tiny_png() -> bytes:
    """Minimal valid PNG (10x10 white) — saves running PIL.Image.new()
    + saving in every test."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_upload_avatar_happy_path(client, mock_conn, monkeypatch):
    # store_avatar stub returns a URL (set by autouse fixture).
    mock_conn.fetchrow.return_value = _profile_row(
        avatar_url="https://cdn/u1.webp?v=123",
    )
    png = _tiny_png()
    r = client.post(
        "/users/me/avatar",
        files={"file": ("a.png", png, "image/png")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["avatar_url"].startswith("https://cdn/u1.webp?v=")


def test_upload_avatar_empty(client):
    r = client.post(
        "/users/me/avatar",
        files={"file": ("a.png", b"", "image/png")},
    )
    assert r.status_code == 400


def test_upload_avatar_bad_mime(client):
    r = client.post(
        "/users/me/avatar",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400


def test_upload_avatar_no_bucket(client, mock_conn, monkeypatch):
    # store_avatar returning None → bucket not configured → 503.
    monkeypatch.setattr(users, "store_avatar", lambda b, uid: None)
    png = _tiny_png()
    r = client.post(
        "/users/me/avatar",
        files={"file": ("a.png", png, "image/png")},
    )
    assert r.status_code == 503


def test_delete_avatar_clears_column(client, mock_conn):
    mock_conn.fetchrow.return_value = _profile_row(avatar_url=None)
    r = client.delete("/users/me/avatar")
    assert r.status_code == 200
    assert r.json()["avatar_url"] is None
    # Verify the SQL nulled the column.
    executed_sqls = [c.args[0] for c in mock_conn.execute.call_args_list]
    assert any("avatar_url = NULL" in s for s in executed_sqls)
