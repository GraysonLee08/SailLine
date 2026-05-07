# backend/tests/test_telemetry.py
"""Tests for the telemetry router.

Uses FastAPI dependency overrides to swap auth + DB for in-memory fakes —
no Firebase, no Postgres. Mirrors the pattern used by the tracks router.

Mocking shape: the endpoint does
    async with pool.acquire() as conn:
        async with conn.transaction():
            ...
so we need TWO async context managers. Both are AsyncMocks whose
__aenter__ resolves to the connection (or None for the transaction).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user
from app.main import app
from app.routers.telemetry import (
    MAX_GPS_SAMPLES_PER_BATCH,
    MAX_IMU_SAMPLES_PER_BATCH,
)


# ─── Constants & fixtures ────────────────────────────────────────────────


FAKE_UID = "test-user-uid"
FAKE_USER = {
    "uid": FAKE_UID,
    "email": "test@example.com",
    "tier": "free",
    "claims": {},
}


@pytest.fixture
def fake_conn() -> MagicMock:
    """Mock asyncpg.Connection.

    Default state: ownership check passes (fetchrow returns a row).
    Override per-test with `fake_conn.fetchrow.return_value = None`
    to simulate a non-owned race for the 404 path.
    """
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"?column?": 1})
    conn.executemany = AsyncMock()
    conn.execute = AsyncMock()

    # `async with conn.transaction():` — transaction() is sync, returns
    # an async context manager. No `as` binding in the router so
    # __aenter__'s return value is irrelevant.
    tx_ctx = AsyncMock()
    conn.transaction = MagicMock(return_value=tx_ctx)
    return conn


@pytest.fixture
def fake_pool(fake_conn: MagicMock) -> MagicMock:
    """Mock asyncpg.Pool whose acquire() yields the fake connection."""
    pool = MagicMock()
    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__.return_value = fake_conn
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


@pytest.fixture
def client(fake_pool: MagicMock):
    """TestClient with both auth and pool dependencies overridden."""
    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    app.dependency_overrides[db.get_pool] = lambda: fake_pool
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client(fake_pool: MagicMock):
    """TestClient with ONLY the pool overridden — auth runs for real.

    Used by the 403 test to confirm HTTPBearer rejects a missing token.
    """
    app.dependency_overrides[db.get_pool] = lambda: fake_pool
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def race_url() -> str:
    return f"/api/races/{uuid4()}/telemetry"


# ─── Payload helpers ─────────────────────────────────────────────────────


def _gps(n: int = 1) -> list[dict]:
    return [
        {
            "t": "2026-05-07T12:00:00Z",
            "lat": 41.9,
            "lon": -87.6,
            "sog_kts": 5.5,
            "cog_deg": 180.0,
            "gps_acc_m": 5.0,
        }
        for _ in range(n)
    ]


def _imu(n: int = 1) -> list[dict]:
    return [
        {
            "t": "2026-05-07T12:00:00Z",
            "heel_deg": 18.0,
            "pitch_deg": 2.0,
            "yaw_deg": 90.0,
        }
        for _ in range(n)
    ]


def _calibration() -> dict:
    return {
        "captured_at": "2026-05-07T11:55:00Z",
        "heel_zero_offset_deg": 1.5,
        "pitch_zero_offset_deg": -0.5,
    }


# ─── Tests ───────────────────────────────────────────────────────────────


def test_post_telemetry_unauthenticated_403(unauth_client: TestClient):
    """No bearer token → HTTPBearer(auto_error=True) returns 403.

    FastAPI's HTTPBearer returns 403 (not 401) for missing credentials.
    Confirmed against the deployed API in the smoke test.
    """
    r = unauth_client.post(
        f"/api/races/{uuid4()}/telemetry",
        json={"gps": [], "imu": []},
    )
    assert r.status_code == 403


def test_post_telemetry_empty_batch_200(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    """Empty batch is accepted as a heartbeat — 0/0/false ack."""
    r = client.post(race_url, json={"gps": [], "imu": []})

    assert r.status_code == 200
    assert r.json() == {
        "gps_inserted": 0,
        "imu_inserted": 0,
        "calibration_inserted": False,
    }
    fake_conn.executemany.assert_not_called()
    fake_conn.execute.assert_not_called()


def test_post_telemetry_gps_only(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    r = client.post(race_url, json={"gps": _gps(3), "imu": []})

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 3
    assert body["imu_inserted"] == 0
    assert body["calibration_inserted"] is False
    assert fake_conn.executemany.call_count == 1
    fake_conn.execute.assert_not_called()


def test_post_telemetry_imu_only(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    r = client.post(race_url, json={"gps": [], "imu": _imu(5)})

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 0
    assert body["imu_inserted"] == 5
    assert body["calibration_inserted"] is False
    assert fake_conn.executemany.call_count == 1
    fake_conn.execute.assert_not_called()


def test_post_telemetry_with_calibration(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    """Calibration uses execute(), not executemany() (single row)."""
    r = client.post(
        race_url,
        json={"gps": [], "imu": [], "calibration": _calibration()},
    )

    assert r.status_code == 200
    assert r.json()["calibration_inserted"] is True
    fake_conn.executemany.assert_not_called()
    fake_conn.execute.assert_called_once()


def test_post_telemetry_full_batch(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    """GPS + IMU + calibration in one batch — the common 'first flush
    after re-zero' shape."""
    r = client.post(
        race_url,
        json={
            "gps": _gps(2),
            "imu": _imu(10),
            "calibration": _calibration(),
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 2
    assert body["imu_inserted"] == 10
    assert body["calibration_inserted"] is True
    # GPS and IMU each get one executemany; calibration is a single execute
    assert fake_conn.executemany.call_count == 2
    fake_conn.execute.assert_called_once()


def test_post_telemetry_gps_over_limit_413(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    """GPS batch above the cap is rejected before any DB work."""
    r = client.post(
        race_url,
        json={"gps": _gps(MAX_GPS_SAMPLES_PER_BATCH + 1), "imu": []},
    )

    assert r.status_code == 413
    fake_conn.fetchrow.assert_not_called()
    fake_conn.executemany.assert_not_called()


def test_post_telemetry_imu_over_limit_413(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    r = client.post(
        race_url,
        json={"gps": [], "imu": _imu(MAX_IMU_SAMPLES_PER_BATCH + 1)},
    )

    assert r.status_code == 413
    fake_conn.fetchrow.assert_not_called()
    fake_conn.executemany.assert_not_called()


def test_post_telemetry_cross_user_404(
    client: TestClient, race_url: str, fake_conn: MagicMock
):
    """Race not owned by caller → 404 (not 403, to avoid leaking
    existence). No inserts attempted past the ownership check."""
    fake_conn.fetchrow.return_value = None

    r = client.post(race_url, json={"gps": _gps(1), "imu": []})

    assert r.status_code == 404
    assert r.json()["detail"] == "race not found"
    fake_conn.executemany.assert_not_called()
    fake_conn.execute.assert_not_called()


def test_post_telemetry_invalid_lat_422(client: TestClient, race_url: str):
    """Pydantic catches lat > 90 before the handler runs."""
    bad = _gps(1)
    bad[0]["lat"] = 91.0

    r = client.post(race_url, json={"gps": bad, "imu": []})
    assert r.status_code == 422


def test_post_telemetry_invalid_heel_422(client: TestClient, race_url: str):
    """Pydantic catches heel > 90 before the handler runs."""
    bad = _imu(1)
    bad[0]["heel_deg"] = 120.0

    r = client.post(race_url, json={"gps": [], "imu": bad})
    assert r.status_code == 422
