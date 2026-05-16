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

After Session E the telemetry router uses ``load_race_for_ingest``
(JOIN-aware predicate, returns marks + mark_passes), bulk-inserts GPS
via ``unnest`` (one ``execute`` call, not ``executemany``), and
delegates mark-rounding + the postprocess trigger to
``app.services.track_ingest``. Tests below cover both the new ack
shape and the regressions Session E was designed to prevent: the
``position`` column name, the ``race_write_predicate`` auth path,
and mark-rounding parity with ``/track``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
from app.services import track_ingest


# ─── Constants & fixtures ────────────────────────────────────────────────


FAKE_UID = "test-user-uid"
FAKE_USER = {
    "uid": FAKE_UID,
    "email": "test@example.com",
    "tier": "free",
    "claims": {},
}


def _race_row(marks=None, mark_passes=None):
    """Build the row shape that ``load_race_for_ingest`` expects.

    Defaults to a single mark deliberately far away from the test GPS
    fixtures so the default test doesn't accidentally trigger
    mark-rounding behaviour. Tests that want rounding pass a closer
    mark explicitly.
    """
    if marks is None:
        marks = [{"name": "Far", "lat": 0.0, "lon": 0.0}]
    if mark_passes is None:
        mark_passes = []
    return {"marks": json.dumps(marks), "mark_passes": json.dumps(mark_passes)}


@pytest.fixture
def fake_conn() -> MagicMock:
    """Mock asyncpg.Connection.

    Default state: ``load_race_for_ingest`` returns a row with a
    far-away mark and no existing passes — so the auth check passes
    and no mark-pass UPDATE fires. Override per-test with
    ``fake_conn.fetchrow.return_value = None`` to simulate a
    non-writeable race (404).
    """
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_race_row())
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


@pytest.fixture
def no_trigger(monkeypatch):
    """Replace the postprocess trigger with an AsyncMock so tests
    don't reach for real ADC. Returns the mock so callers can assert
    on its call count.
    """
    fake = AsyncMock()
    monkeypatch.setattr(track_ingest, "trigger_race_postprocess", fake)
    return fake


# ─── Payload helpers ─────────────────────────────────────────────────────


def _gps(n: int = 1) -> list[dict]:
    """GPS sample at a fixed (lat, lon) far from the default mark."""
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


def _rounding_gps_batch(mark_lat: float, mark_lon: float, n: int = 9) -> list[dict]:
    """A GPS batch designed to enter and exit a 50m radius around
    (mark_lat, mark_lon) — same shape as the tracks router tests.

    Starts ~70m west of the mark and walks east through the radius;
    the middle of the batch is inside the 50m default radius, the
    tail is back outside, so the detector emits one rounding.
    """
    base = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    return [
        {
            "t": (base + timedelta(seconds=i * 5)).isoformat(),
            "lat": mark_lat,
            "lon": mark_lon - 0.0009 + i * 0.000225,
            "sog_kts": 5.0,
            "cog_deg": 90.0,
            "gps_acc_m": 4.0,
        }
        for i in range(n)
    ]


# ─── Auth & validation ─────────────────────────────────────────────────


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


def test_post_telemetry_cross_user_404(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """Race not writeable by caller → 404 (not 403, to avoid leaking
    existence). No inserts attempted past the auth check."""
    fake_conn.fetchrow.return_value = None

    r = client.post(race_url, json={"gps": _gps(1), "imu": []})

    assert r.status_code == 404
    assert r.json()["detail"] == "race not found"
    fake_conn.executemany.assert_not_called()
    fake_conn.execute.assert_not_called()
    no_trigger.assert_not_awaited()


def test_post_telemetry_uses_race_write_predicate(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """Regression guard: the auth read must use ``race_write_predicate``
    (boat_crew aware), NOT the pre-D3 ``user_id = $2`` shape.

    Without this, crew members on a shared boat would silently lose
    the ability to record telemetry the moment the frontend migrates
    to ``/telemetry``.
    """
    r = client.post(race_url, json={"gps": _gps(1), "imu": []})
    assert r.status_code == 200
    auth_sql = fake_conn.fetchrow.await_args.args[0]
    assert "boat_crew" in auth_sql
    assert "bc.role IN ('owner', 'crew')" in auth_sql


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
    fake_conn.execute.assert_not_called()


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
    fake_conn.execute.assert_not_called()


# ─── Successful batches ───────────────────────────────────────────────


def test_post_telemetry_empty_batch_200(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """Empty batch is accepted as a heartbeat — 0/0/false ack with
    empty mark_passes lists."""
    r = client.post(race_url, json={"gps": [], "imu": []})

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 0
    assert body["imu_inserted"] == 0
    assert body["calibration_inserted"] is False
    assert body["mark_passes"] == []
    assert body["new_mark_passes"] == []
    fake_conn.executemany.assert_not_called()
    fake_conn.execute.assert_not_called()
    no_trigger.assert_not_awaited()


def test_post_telemetry_gps_only(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """GPS-only batch: single ``execute`` for the INSERT, no
    ``executemany``, no mark-pass UPDATE (default fixture has a
    far-away mark)."""
    r = client.post(race_url, json={"gps": _gps(3), "imu": []})

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 3
    assert body["imu_inserted"] == 0
    assert body["calibration_inserted"] is False
    assert body["mark_passes"] == []
    assert body["new_mark_passes"] == []
    fake_conn.executemany.assert_not_called()
    assert fake_conn.execute.await_count == 1
    no_trigger.assert_not_awaited()


def test_post_telemetry_inserts_into_position_column(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """Regression for the Session E bug: the INSERT statement must
    name the ``position`` column, NOT ``location`` (which does not
    exist — migration 0002).

    Without this guard, the endpoint would 500 the first time a real
    client posts against a real database, but every mocked test
    would still pass.
    """
    r = client.post(race_url, json={"gps": _gps(2), "imu": []})
    assert r.status_code == 200
    insert_sql = fake_conn.execute.await_args.args[0]
    assert "INSERT INTO track_points" in insert_sql
    assert "position" in insert_sql
    assert "location" not in insert_sql
    assert "ST_SetSRID(ST_MakePoint" in insert_sql
    assert "unnest" in insert_sql.lower()


def test_post_telemetry_imu_only(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """IMU-only batch uses ``executemany``; no GPS insert means no
    ``execute`` call against the connection."""
    r = client.post(race_url, json={"gps": [], "imu": _imu(5)})

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 0
    assert body["imu_inserted"] == 5
    assert body["calibration_inserted"] is False
    assert fake_conn.executemany.call_count == 1
    fake_conn.execute.assert_not_called()
    no_trigger.assert_not_awaited()


def test_post_telemetry_with_calibration(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """Calibration-only batch uses a single ``execute`` for the
    INSERT into ``race_calibrations`` — no GPS, no IMU."""
    r = client.post(
        race_url,
        json={"gps": [], "imu": [], "calibration": _calibration()},
    )

    assert r.status_code == 200
    assert r.json()["calibration_inserted"] is True
    fake_conn.executemany.assert_not_called()
    assert fake_conn.execute.await_count == 1
    no_trigger.assert_not_awaited()


def test_post_telemetry_full_batch(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """GPS + IMU + calibration — the common 'first flush after re-zero'
    shape.

    Call counts after Session E:
      * ``execute`` x2 — GPS unnest INSERT + calibration INSERT
        (no mark-pass UPDATE: default fixture mark is far away)
      * ``executemany`` x1 — IMU rows
    """
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
    assert body["mark_passes"] == []
    assert body["new_mark_passes"] == []
    assert fake_conn.executemany.call_count == 1
    assert fake_conn.execute.await_count == 2
    no_trigger.assert_not_awaited()


# ─── Mark-rounding parity with /track ─────────────────────────────────


def test_post_telemetry_emits_mark_pass(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """A GPS batch that crosses a mark must emit a ``new_mark_passes``
    entry AND persist via UPDATE — same semantics as ``/track``.

    The default fixture mark is far away, so this test overrides it
    to a mark the rounding batch helper walks through.
    """
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    fake_conn.fetchrow.return_value = _race_row(marks=[mark])

    r = client.post(
        race_url,
        json={"gps": _rounding_gps_batch(mark["lat"], mark["lon"]), "imu": []},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 9
    assert len(body["new_mark_passes"]) == 1
    assert body["new_mark_passes"][0]["mark_index"] == 0
    assert body["mark_passes"] == body["new_mark_passes"]
    # Two execute calls: GPS INSERT then mark-pass UPDATE.
    assert fake_conn.execute.await_count == 2
    update_call = fake_conn.execute.await_args_list[1].args
    assert "UPDATE race_sessions" in update_call[0]
    assert "mark_passes" in update_call[0]
    persisted = json.loads(update_call[1])
    assert len(persisted) == 1
    assert persisted[0]["mark_index"] == 0


def test_post_telemetry_resumes_from_existing_passes(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """If a prior batch already rounded mark 0, this batch's rounding
    of mark 0 (offline-queue replay) should NOT create a duplicate
    pass — detector resumes at next-unrounded index."""
    marks = [
        {"name": "A", "lat": 42.30, "lon": -87.80},
        {"name": "B", "lat": 42.31, "lon": -87.80},
    ]
    existing = [
        {
            "mark_index": 0,
            "ts": "2026-05-14T17:55:00+00:00",
            "lat": 42.30,
            "lon": -87.80,
        }
    ]
    fake_conn.fetchrow.return_value = _race_row(
        marks=marks, mark_passes=existing
    )

    r = client.post(
        race_url,
        json={
            "gps": _rounding_gps_batch(marks[0]["lat"], marks[0]["lon"]),
            "imu": [],
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["new_mark_passes"] == []
    assert len(body["mark_passes"]) == 1
    assert body["mark_passes"][0]["mark_index"] == 0


# ─── Postprocess trigger ──────────────────────────────────────────────


def test_post_telemetry_triggers_postprocess_at_final_mark(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """A batch that rounds the LAST mark of a single-mark course
    kicks off the ``race-postprocess`` Cloud Run Job."""
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    fake_conn.fetchrow.return_value = _race_row(marks=[mark])

    r = client.post(
        race_url,
        json={
            "gps": _rounding_gps_batch(mark["lat"], mark["lon"]),
            "imu": [],
        },
    )

    assert r.status_code == 200
    assert no_trigger.await_count == 1


def test_post_telemetry_does_not_trigger_intermediate_mark(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """Beer-can layouts: a batch that rounds only mark 0 on a two-mark
    course must not fire the trigger."""
    marks = [
        {"name": "A", "lat": 42.30, "lon": -87.80},
        {"name": "B", "lat": 42.40, "lon": -87.80},
    ]
    fake_conn.fetchrow.return_value = _race_row(marks=marks)

    r = client.post(
        race_url,
        json={
            "gps": _rounding_gps_batch(marks[0]["lat"], marks[0]["lon"]),
            "imu": [],
        },
    )

    assert r.status_code == 200
    no_trigger.assert_not_awaited()


def test_post_telemetry_does_not_trigger_when_no_new_passes(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """Re-flushed batch (no new roundings) on a completed race must
    not re-fire the job."""
    r = client.post(race_url, json={"gps": _gps(3), "imu": []})

    assert r.status_code == 200
    no_trigger.assert_not_awaited()
