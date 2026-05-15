"""Tests for app/routers/boats.py.

Same pattern as test_race_stats_router.py: override db.get_pool and
get_current_user, stub asyncpg with AsyncMock. The cert upload
endpoint is exercised against the real Gaucho fixture PDF so we know
the round trip (multipart in → parser → response shape) holds.
"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user
from app.routers import boats


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def user():
    return {"uid": "u1", "email": "t@x", "tier": "free", "claims": {}}


@pytest.fixture
def other_user():
    return {"uid": "other", "email": "y@x", "tier": "free", "claims": {}}


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
    app.include_router(boats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


@pytest.fixture
def client(user, mock_conn):
    return TestClient(_make_app(user, mock_conn))


# Stub the GCS helpers — keep tests independent of credentials.
@pytest.fixture(autouse=True)
def _stub_gcs(monkeypatch):
    monkeypatch.setattr(
        boats, "_try_store_gcs", lambda *a, **kw: None,
    )
    monkeypatch.setattr(boats, "_try_delete_gcs", lambda *a, **kw: None)


def _boat_row(**overrides):
    base = {
        "id": uuid4(),
        "owner_id": "u1",
        "name": "Test boat",
        "sail_number": "1",
        "yacht_type": None, "year": None, "mwphrf_region": None,
        "loa": None, "lwl": None, "beam": None, "draft": None,
        "displacement": None,
        "engine": None, "prop_install": None, "prop_type": None,
        "p": None, "e": None, "i": None, "j": None,
        "isp": None, "spl": None, "jc_tps": None,
        "hcp": None, "dhcp": None, "nshcp": None, "dnshcp": None,
        "cert_number": None, "cert_issued_on": None,
        "cert_pdf_gcs_url": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return base


# ─── List ────────────────────────────────────────────────────────────


def test_list_returns_user_boats(client, mock_conn):
    mock_conn.fetch.return_value = [_boat_row(), _boat_row(name="Boat 2")]
    r = client.get("/api/boats")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["name"] == "Test boat"


def test_list_empty(client, mock_conn):
    mock_conn.fetch.return_value = []
    r = client.get("/api/boats")
    assert r.status_code == 200
    assert r.json() == []


# ─── Create ──────────────────────────────────────────────────────────


def test_create_requires_name(client, mock_conn):
    r = client.post("/api/boats", json={})
    assert r.status_code == 422   # missing required name


def test_create_returns_boat(client, mock_conn):
    mock_conn.fetchrow.return_value = _boat_row(name="Gaucho", hcp=75)
    r = client.post("/api/boats", json={"name": "Gaucho", "hcp": 75})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Gaucho"
    assert body["hcp"] == 75


# ─── Get / Update / Delete ──────────────────────────────────────────


def test_get_404_when_not_found(client, mock_conn):
    mock_conn.fetchrow.return_value = None
    r = client.get(f"/api/boats/{uuid4()}")
    assert r.status_code == 404


def test_update_404_when_not_owned(client, mock_conn):
    mock_conn.fetchrow.return_value = None
    r = client.patch(f"/api/boats/{uuid4()}", json={"hcp": 80})
    assert r.status_code == 404


def test_update_returns_updated_boat(client, mock_conn):
    bid = uuid4()
    # First fetchrow → ownership pass; second → returning row.
    mock_conn.fetchrow.side_effect = [
        _boat_row(id=bid),
        _boat_row(id=bid, hcp=80),
    ]
    r = client.patch(f"/api/boats/{bid}", json={"hcp": 80})
    assert r.status_code == 200
    assert r.json()["hcp"] == 80


def test_update_empty_body_is_noop(client, mock_conn):
    bid = uuid4()
    mock_conn.fetchrow.return_value = _boat_row(id=bid)
    r = client.patch(f"/api/boats/{bid}", json={})
    assert r.status_code == 200
    # Only one fetchrow (the ownership check); no UPDATE.
    assert mock_conn.fetchrow.await_count == 1


def test_delete_204(client, mock_conn):
    bid = uuid4()
    mock_conn.fetchrow.return_value = _boat_row(id=bid)
    r = client.delete(f"/api/boats/{bid}")
    assert r.status_code == 204


# ─── Cert upload ─────────────────────────────────────────────────────


FIXTURE = Path(__file__).parent / "fixtures" / "mwphrf_gaucho.pdf"


@pytest.mark.skipif(not FIXTURE.exists(), reason="cert fixture missing")
def test_cert_upload_parses_and_returns_fields(client, mock_conn):
    bid = uuid4()
    mock_conn.fetchrow.return_value = _boat_row(id=bid)
    pdf_bytes = FIXTURE.read_bytes()
    r = client.post(
        f"/api/boats/{bid}/cert",
        files={"file": ("gaucho.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["parse_succeeded"] is True
    assert body["parsed"]["name"] == "Gaucho"
    assert body["parsed"]["hcp"] == 75
    # GCS stub returned None, so no URL persisted.
    assert body["stored_url"] is None


def test_cert_upload_empty_body_rejected(client, mock_conn):
    bid = uuid4()
    mock_conn.fetchrow.return_value = _boat_row(id=bid)
    r = client.post(
        f"/api/boats/{bid}/cert",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert r.status_code == 400


def test_cert_upload_oversized_rejected(client, mock_conn):
    bid = uuid4()
    mock_conn.fetchrow.return_value = _boat_row(id=bid)
    too_big = b"\x00" * (6 * 1024 * 1024)
    r = client.post(
        f"/api/boats/{bid}/cert",
        files={"file": ("big.pdf", too_big, "application/pdf")},
    )
    assert r.status_code == 413


def test_cert_upload_404_when_boat_not_owned(client, mock_conn):
    mock_conn.fetchrow.return_value = None
    r = client.post(
        f"/api/boats/{uuid4()}/cert",
        files={"file": ("x.pdf", b"%PDF-1.4\n...", "application/pdf")},
    )
    assert r.status_code == 404


def test_cert_upload_non_pdf_returns_parse_failed(client, mock_conn):
    bid = uuid4()
    mock_conn.fetchrow.return_value = _boat_row(id=bid)
    r = client.post(
        f"/api/boats/{bid}/cert",
        files={"file": ("x.txt", b"not a pdf at all", "application/pdf")},
    )
    # Parser returns empty; endpoint still 200s — caller decides what
    # to do with parse_succeeded=False.
    assert r.status_code == 200
    body = r.json()
    assert body["parse_succeeded"] is False
