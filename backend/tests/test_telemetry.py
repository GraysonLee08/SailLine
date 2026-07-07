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


def _race_row(
    marks=None,
    mark_passes=None,
    started_at=None,
    start_at=None,
    mode="distance",
    detector_state=None,
):
    """Build the row shape that ``load_race_for_ingest`` expects.

    Defaults to a single mark deliberately far away from the test GPS
    fixtures so the default test doesn't accidentally trigger
    mark-rounding behaviour. Tests that want rounding pass a closer
    mark explicitly.

    ``started_at`` / ``start_at`` were added when the recorder lifecycle
    columns landed (2026-05-26). They default to None — same shape as a
    freshly-created race with no scheduled gun time and no first
    telemetry POST yet. Tests that exercise the backfill path override
    them explicitly.

    ``mode`` was added in v3 (2026-05-30) so the detector picks the
    right per-mark thresholds. Defaults to "distance" — the wider
    tolerance, mirroring the production safer-default.

    ``detector_state`` was added in 0020 (2026-06-04) for cross-batch
    traversal persistence. Defaults to None — the "fresh traversal"
    sentinel, matching a newly-created race.
    """
    if marks is None:
        marks = [{"name": "Far", "lat": 0.0, "lon": 0.0}]
    if mark_passes is None:
        mark_passes = []
    return {
        "marks": json.dumps(marks),
        "mark_passes": json.dumps(mark_passes),
        "started_at": started_at,
        "start_at": start_at,
        "mode": mode,
        "detector_state": detector_state,
        # v4 line-bearing columns (0022). None → CPA fallback for the
        # start/finish marks — the v3 behaviour these tests assume.
        "start_line_bearing_override": None,
        "start_line_bearing_deg": None,
    }


@pytest.fixture
def fake_conn() -> MagicMock:
    """Mock asyncpg.Connection.

    Default state: ``load_race_for_ingest`` returns a row with a
    far-away mark and no existing passes — so the auth check passes
    and no mark-pass UPDATE fires. Override per-test with
    ``fake_conn.fetchrow.return_value = None`` to simulate a
    non-writeable race (404).

    The GPS and IMU INSERTs use ``conn.fetch(... RETURNING 1)`` so the
    handler can count rows that actually landed (idempotency contract,
    migration 0018 — 2026-06-01). Default fixture pretends every row
    landed, by returning N rows from each fetch where N matches the
    incoming unnest array length. Tests that exercise the idempotent-
    duplicate path override ``fake_conn.fetch.side_effect`` to return
    fewer rows than were sent.

    ``conn.execute`` remains the path for the mark-pass UPDATE only.
    A test that asserts ``execute`` was not awaited is asserting that
    detection didn't trigger an UPDATE (e.g., no rounding occurred),
    independent of whether the INSERTs fired.
    """
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_race_row())
    conn.executemany = AsyncMock()
    conn.execute = AsyncMock()

    async def _fetch_all_landed(sql: str, *args, **kwargs):
        """Default behaviour: every unnested row reports as inserted.

        Inspects the SQL to decide which array argument represents the
        row count. For the GPS INSERT the recorded_at array is the 2nd
        positional arg ($2 in SQL). For IMU it's also $2. Both end up
        at ``args[1]`` after the race_id at ``args[0]``.
        """
        if "INSERT INTO track_points" in sql or "INSERT INTO imu_samples" in sql:
            if len(args) >= 2 and isinstance(args[1], list):
                return [{"?column?": 1}] * len(args[1])
        return []

    conn.fetch = AsyncMock(side_effect=_fetch_all_landed)

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
    """A GPS batch designed to enter and exit the rounding radius around
    (mark_lat, mark_lon).

    Originally tuned for the 50m radius. After 2026-05-26 a single-mark
    course gets `FINAL_MARK_RADIUS_M = 75m` via `radii_for_course(1)`,
    so the span was widened to ±150m: well outside the 75m zone at the
    endpoints, on the mark at the middle. The middle samples are inside
    either radius; the tail is back outside, so the detector emits one
    rounding regardless of which radius the production code picked.
    """
    base = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    span = 0.0018  # ~150m at 42° lat — clears the 75m final-mark radius
    step = (2 * span) / (n - 1)
    return [
        {
            "t": (base + timedelta(seconds=i * 5)).isoformat(),
            "lat": mark_lat,
            "lon": mark_lon - span + i * step,
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
    fake_conn.fetch.assert_not_called()
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


def test_post_telemetry_load_uses_for_update(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """The race-row read MUST acquire ``FOR UPDATE`` so concurrent
    batches against the same race serialize on the row lock.

    Regression guard for the 2026-06-01 concurrency fix: without
    ``FOR UPDATE`` the read-modify-write on ``mark_passes`` can race
    when the recorder's durable queue retries a batch in parallel
    with a fresh online batch.
    """
    r = client.post(race_url, json={"gps": _gps(1), "imu": []})
    assert r.status_code == 200
    auth_sql = fake_conn.fetchrow.await_args.args[0]
    assert "FOR UPDATE" in auth_sql


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
    fake_conn.fetch.assert_not_called()
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
    fake_conn.fetch.assert_not_called()
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
    fake_conn.fetch.assert_not_called()
    fake_conn.execute.assert_not_called()
    no_trigger.assert_not_awaited()


def test_post_telemetry_gps_only(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """GPS-only batch: single ``fetch`` for the INSERT (RETURNING 1),
    one ``execute`` (UPDATE) to persist the detector traversal state
    for the next batch (migration 0020 — 2026-06-04). No mark_passes
    UPDATE because the default fixture mark is far away. No
    ``executemany`` because IMU is empty.

    Pre-0020 this test asserted ``execute.assert_not_called()``. The
    cross-batch state persistence work makes every batch hit the
    UPDATE so the next batch can resume; without that, depart-confirm
    counters reset across small batches and Mark-2-style misses come
    back (see sailline-docs/2026-06-04_session.md, Item 5).

    ``gps_inserted`` mirrors the row count returned by the INSERT,
    which the fixture defaults to "every sent row landed."
    """
    r = client.post(race_url, json={"gps": _gps(3), "imu": []})

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 3
    assert body["imu_inserted"] == 0
    assert body["calibration_inserted"] is False
    assert body["mark_passes"] == []
    assert body["new_mark_passes"] == []
    fake_conn.executemany.assert_not_called()
    assert fake_conn.fetch.await_count == 1
    # Single UPDATE persists detector_state. Must target the
    # detector_state column and must NOT include mark_passes (no
    # new passes detected in this batch).
    assert fake_conn.execute.await_count == 1
    update_sql = fake_conn.execute.await_args.args[0]
    assert "detector_state" in update_sql
    assert "mark_passes" not in update_sql
    no_trigger.assert_not_awaited()


def test_post_telemetry_inserts_into_position_column(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """Regression for the Session E bug: the INSERT statement must
    name the ``position`` column, NOT ``location`` (which does not
    exist — migration 0002).

    Also a regression guard for the 2026-06-01 idempotency change —
    the INSERT must include ``ON CONFLICT (session_id, recorded_at)
    DO NOTHING`` and ``RETURNING 1`` so the durable-queue recorder
    can safely re-send batches.

    Without these guards, the endpoint would 500 the first time a real
    client posts against a real database, but every mocked test
    would still pass.
    """
    r = client.post(race_url, json={"gps": _gps(2), "imu": []})
    assert r.status_code == 200
    insert_sql = fake_conn.fetch.await_args.args[0]
    assert "INSERT INTO track_points" in insert_sql
    assert "position" in insert_sql
    assert "location" not in insert_sql
    assert "ST_SetSRID(ST_MakePoint" in insert_sql
    assert "unnest" in insert_sql.lower()
    assert "ON CONFLICT (session_id, recorded_at) DO NOTHING" in insert_sql
    assert "RETURNING 1" in insert_sql


def test_post_telemetry_imu_only(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """IMU-only batch uses ``fetch`` for the unnest INSERT (RETURNING
    1); no GPS insert means no second ``fetch``, no ``execute``."""
    r = client.post(race_url, json={"gps": [], "imu": _imu(5)})

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 0
    assert body["imu_inserted"] == 5
    assert body["calibration_inserted"] is False
    fake_conn.executemany.assert_not_called()
    assert fake_conn.fetch.await_count == 1
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
    fake_conn.fetch.assert_not_called()
    assert fake_conn.execute.await_count == 1
    no_trigger.assert_not_awaited()


def test_post_telemetry_full_batch(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """GPS + IMU + calibration — the common 'first flush after re-zero'
    shape.

    Call counts after the 2026-06-04 cross-batch detector-state work
    (migration 0020):
      * ``fetch`` x2 — GPS unnest INSERT (RETURNING 1) + IMU unnest
        INSERT (RETURNING 1). Both report actual landed counts.
      * ``execute`` x2 — calibration INSERT + detector_state UPDATE.
        The detector_state UPDATE persists the traversal state for
        the next batch even when no new pass was detected (default
        fixture mark is far so no mark_passes UPDATE fires).

    Pre-0020 this asserted ``execute.await_count == 1`` (just
    calibration). The state-persistence UPDATE is now unconditional;
    see sailline-docs/2026-06-04_session.md, Item 5.
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
    fake_conn.executemany.assert_not_called()
    assert fake_conn.fetch.await_count == 2
    assert fake_conn.execute.await_count == 2
    # Exactly one of the two executes is the detector_state UPDATE.
    update_sqls = [c.args[0] for c in fake_conn.execute.await_args_list]
    assert sum("detector_state" in s for s in update_sqls) == 1
    no_trigger.assert_not_awaited()


# ─── Mark-rounding parity with /track ─────────────────────────────────


def test_post_telemetry_emits_mark_pass(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """A GPS batch that crosses a mark must emit a ``new_mark_passes``
    entry AND persist via UPDATE — same semantics as ``/track``.

    The default fixture mark is far away, so this test overrides it
    to a mark the rounding batch helper walks through.

    Call topology after the 2026-06-01 idempotency change: the GPS
    INSERT uses ``fetch`` (RETURNING 1) and the mark-pass UPDATE uses
    ``execute``. So fetch=1, execute=1 — not the old execute=2.
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
    assert fake_conn.fetch.await_count == 1
    assert fake_conn.execute.await_count == 1
    update_call = fake_conn.execute.await_args.args
    assert "UPDATE race_sessions" in update_call[0]
    assert "mark_passes" in update_call[0]
    # Plain list to the ::jsonb param (codec encodes once) — not a string.
    persisted = update_call[1]
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


# ─── Idempotency (2026-06-01, migration 0018) ─────────────────────────


def test_post_telemetry_reports_actual_landed_count(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """When the INSERT's ON CONFLICT clause skips a duplicate, the
    response's ``gps_inserted`` must reflect the rows that ACTUALLY
    landed, not the rows the client SENT.

    The durable-queue recorder relies on this: it treats a 200 with
    ``gps_inserted < len(sent)`` as 'these specific points were
    duplicates — safe to drop from the queue', and a 200 with
    ``gps_inserted == 0`` as 'whole batch was already stored —
    nothing to do.'
    """

    async def _half_landed(sql: str, *args, **kwargs):
        # Simulate ON CONFLICT skipping every other row. The handler
        # sent 4 GPS samples; we return 2 RETURNING rows.
        if "INSERT INTO track_points" in sql:
            return [{"?column?": 1}, {"?column?": 1}]
        return []

    fake_conn.fetch.side_effect = _half_landed

    r = client.post(race_url, json={"gps": _gps(4), "imu": []})

    assert r.status_code == 200
    body = r.json()
    # Server reports the 2 rows that actually landed, not the 4 sent.
    assert body["gps_inserted"] == 2


def test_post_telemetry_reports_zero_when_full_duplicate_batch(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """A re-send of an already-stored batch returns 200 with
    ``gps_inserted=0``. This is the recover-from-lost-ack path: the
    server processed the original batch and committed, but the
    response didn't make it back to the client. The client retries
    the same batch and learns it's already stored — drop from queue,
    no duplicates created.
    """

    async def _all_duplicates(sql: str, *args, **kwargs):
        if "INSERT INTO track_points" in sql:
            return []  # zero RETURNING rows = every sample was a duplicate
        return []

    fake_conn.fetch.side_effect = _all_duplicates

    r = client.post(race_url, json={"gps": _gps(3), "imu": []})

    assert r.status_code == 200
    body = r.json()
    assert body["gps_inserted"] == 0


def test_post_telemetry_imu_insert_uses_on_conflict(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """IMU INSERT must also use ON CONFLICT DO NOTHING with the same
    (session_id, recorded_at) key. Regression guard for the IMU side
    of migration 0018.
    """
    r = client.post(race_url, json={"gps": [], "imu": _imu(2)})

    assert r.status_code == 200
    imu_sql = fake_conn.fetch.await_args.args[0]
    assert "INSERT INTO imu_samples" in imu_sql
    assert "ON CONFLICT (session_id, recorded_at) DO NOTHING" in imu_sql
    assert "RETURNING 1" in imu_sql


# ─── Phase 4 — native uploader sentinel coercion ──────────────────────


def test_post_telemetry_coerces_negative_sentinels(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """The Transistorsoft native uploader emits -1 for speed / heading /
    accuracy when the underlying provider hasn't computed a value.
    The pydantic pre-validator coerces these to null so a single
    sentinel sample can't 422 the whole batch.

    Phase 4 regression guard — without this, the durable-queue
    contract breaks on any sample with missing speed (typical for the
    first 1-2 fixes after start).
    """
    sample_with_sentinels = {
        "t": "2026-05-07T12:00:00Z",
        "lat": 41.9,
        "lon": -87.6,
        "sog_kts": -1.0,
        "cog_deg": -1.0,
        "gps_acc_m": -1.0,
    }
    r = client.post(
        race_url,
        json={"gps": [sample_with_sentinels], "imu": []},
    )

    assert r.status_code == 200
    # The handler still inserted (fixture's default fetch returns one
    # landed row); the negative sentinels reached the INSERT as null.
    assert r.json()["gps_inserted"] == 1
    insert_args = fake_conn.fetch.await_args.args
    # Sog, cog, acc arrays land at args[5], [6], [7] after the SQL,
    # race_id, ts, lat, lon arrays.
    sog_arr, cog_arr, acc_arr = insert_args[5], insert_args[6], insert_args[7]
    assert sog_arr == [None]
    assert cog_arr == [None]
    assert acc_arr == [None]


def test_post_telemetry_accepts_valid_speeds_unchanged(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """Sentinel coercion must NOT eat real values. A positive speed
    flows through unchanged into the INSERT."""
    good_sample = {
        "t": "2026-05-07T12:00:00Z",
        "lat": 41.9,
        "lon": -87.6,
        "sog_kts": 6.5,
        "cog_deg": 180.0,
        "gps_acc_m": 5.0,
    }
    r = client.post(race_url, json={"gps": [good_sample], "imu": []})

    assert r.status_code == 200
    insert_args = fake_conn.fetch.await_args.args
    assert insert_args[5] == [6.5]
    assert insert_args[6] == [180.0]
    assert insert_args[7] == [5.0]


def test_post_telemetry_coerces_string_sentinels(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """v5 string sentinels (2026-07-07 — the zero-upload native bug).

    Transistorsoft v5 reports missing speed/heading/accuracy as
    ``undefined`` (not v4's ``-1``), so the native locationTemplate now
    QUOTES those fields: a missing value renders as ``""`` and a
    present value as a numeric string like ``"12.3"``. The validator
    must null the former, parse the latter, and treat non-numeric junk
    in these OPTIONAL fields as null — never 422 the batch.
    """
    samples = [
        {  # all three missing → nulls
            "t": "2026-07-07T17:30:00Z", "lat": 41.9, "lon": -87.6,
            "sog_kts": "", "cog_deg": "NaN", "gps_acc_m": "undefined",
        },
        {  # numeric strings → parsed floats
            "t": "2026-07-07T17:30:01Z", "lat": 41.9, "lon": -87.6,
            "sog_kts": "6.5", "cog_deg": "180.0", "gps_acc_m": "5.0",
        },
        {  # uninterpolated template junk in an optional field → null
            "t": "2026-07-07T17:30:02Z", "lat": 41.9, "lon": -87.6,
            "sog_kts": "<%= speed * 1.943844 %>",
        },
    ]
    r = client.post(race_url, json={"gps": samples, "imu": []})

    assert r.status_code == 200
    insert_args = fake_conn.fetch.await_args.args
    assert insert_args[5] == [None, 6.5, None]
    assert insert_args[6] == [None, 180.0, None]
    assert insert_args[7] == [None, 5.0, None]


def test_post_telemetry_strict_fields_still_reject_garbage(
    client: TestClient, race_url: str, fake_conn: MagicMock, no_trigger
):
    """The lenient coercion applies ONLY to the optional trio. A junk
    timestamp or latitude must still 422 — silently nulling those
    would store corrupt fixes."""
    bad = {"t": "not-a-time", "lat": 41.9, "lon": -87.6}
    r = client.post(race_url, json={"gps": [bad], "imu": []})
    assert r.status_code == 422
