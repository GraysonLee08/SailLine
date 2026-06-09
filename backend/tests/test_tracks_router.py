"""Tests for app/routers/tracks.py.

Mocks the asyncpg pool by overriding the FastAPI db.get_pool dependency
and stubbing get_current_user. No real database is touched.

POST tests exercise the mark-rounding side effect: the row read returns
marks + existing passes, the helper invokes the detector against the
new batch, and persists any new passes via UPDATE.

Mark-rounding and the post-process job trigger are delegated to
``app.services.track_ingest`` — tests monkeypatch the helper's bound
name for ``trigger_race_postprocess`` (NOT the router's), since after
the Session E refactor the router no longer imports the trigger
directly.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user
from app.routers import tracks
from app.services import track_ingest


# --- Fixtures ----------------------------------------------------------


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
    """Mock asyncpg.Connection.

    Since the 2026-06-01 idempotency rework:
      * The GPS INSERT calls ``conn.fetch`` (not ``execute``) with
        ``ON CONFLICT DO NOTHING RETURNING 1`` so the handler can count
        actually-landed rows. Default fixture pretends every row landed
        — tests that need partial-duplicate behaviour override
        ``mock_conn.fetch.side_effect``.
      * The handler wraps its work in ``conn.transaction()`` so the
        ``FOR UPDATE`` row lock from ``load_race_for_ingest`` is held
        across the read-modify-write of ``mark_passes``.
    """
    conn = AsyncMock()

    async def _fetch_all_landed(sql: str, *args, **kwargs):
        if "INSERT INTO track_points" in sql:
            # The unnested recorded_at array is at args[1] (after
            # race_id at args[0]). Default to "every row landed."
            if len(args) >= 2 and isinstance(args[1], list):
                return [{"?column?": 1}] * len(args[1])
        return []

    conn.fetch = AsyncMock(side_effect=_fetch_all_landed)

    # `async with conn.transaction():` — transaction() is sync, returns
    # an async context manager. AsyncMock auto-supports __aenter__ /
    # __aexit__, so a single AsyncMock() satisfies the context.
    conn.transaction = MagicMock(return_value=AsyncMock())
    return conn


@pytest.fixture
def app(fake_user, mock_conn):
    @asynccontextmanager
    async def fake_acquire():
        yield mock_conn

    pool = MagicMock()
    pool.acquire = fake_acquire

    app = FastAPI()
    app.include_router(tracks.router)
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _race_row(
    marks=None,
    mark_passes=None,
    started_at=None,
    start_at=None,
    mode="distance",
    detector_state=None,
):
    if marks is None:
        marks = [{"name": "M", "lat": 42.30, "lon": -87.80}]
    if mark_passes is None:
        mark_passes = []
    return {
        "marks": json.dumps(marks),
        "mark_passes": json.dumps(mark_passes),
        "started_at": started_at,
        "start_at": start_at,
        "mode": mode,
        # detector_state added in 0020 for cross-batch traversal
        # persistence. None == "fresh traversal" (default for a new race).
        "detector_state": detector_state,
    }


def _owned():
    return {"?column?": 1}


def _sample_points(n=3, start=None):
    start = start or datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    return [
        {
            "recorded_at": (start + timedelta(seconds=i)).isoformat(),
            "lat": 42.30 + i * 0.0001,
            "lon": -87.80 + i * 0.0001,
            "speed_kts": 5.5 + i * 0.1,
            "heading_deg": 180.0,
        }
        for i in range(n)
    ]


# --- POST batch insert -------------------------------------------------


def test_post_batch_inserts(client, mock_conn):
    """Plain bulk INSERT path.

    After 2026-06-01 the INSERT runs via ``conn.fetch`` (RETURNING 1
    for the idempotency contract) and the handler reports the rows
    that actually landed. Default fixture pretends every sent row
    landed — see the ``mock_conn`` fixture docstring.
    """
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}]
    )
    race_id = uuid4()
    points = _sample_points(5)

    r = client.post(f"/api/races/{race_id}/track", json={"points": points})

    assert r.status_code == 201
    body = r.json()
    assert body["inserted"] == 5
    assert body["new_mark_passes"] == []
    assert body["mark_passes"] == []
    mock_conn.fetchrow.assert_awaited_once()
    mock_conn.fetch.assert_awaited_once()
    args = mock_conn.fetch.await_args.args
    sql = args[0]
    assert "INSERT INTO track_points" in sql
    assert "unnest" in sql.lower()
    assert "ST_SetSRID(ST_MakePoint" in sql
    assert "ON CONFLICT (session_id, recorded_at) DO NOTHING" in sql
    assert "RETURNING 1" in sql
    assert args[1] == race_id
    for arr in args[2:]:
        assert len(arr) == 5
    own_args = mock_conn.fetchrow.await_args.args
    assert own_args[1] == race_id
    assert own_args[2] == "test-uid"


def test_post_uses_race_write_predicate(client, mock_conn):
    """The auth read must use race_write_predicate (boat_crew aware),
    not the pre-D3 owner-only check.

    Regression guard: a refactor that flips back to ``user_id = $2``
    would silently break crew recording on shared boats.
    """
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}]
    )

    r = client.post(
        f"/api/races/{uuid4()}/track", json={"points": _sample_points(1)}
    )
    assert r.status_code == 201
    auth_sql = mock_conn.fetchrow.await_args.args[0]
    assert "boat_crew" in auth_sql
    assert "bc.role IN ('owner', 'crew')" in auth_sql


def test_post_404_when_race_not_owned(client, mock_conn):
    mock_conn.fetchrow.return_value = None

    r = client.post(
        f"/api/races/{uuid4()}/track",
        json={"points": _sample_points(2)},
    )

    assert r.status_code == 404
    mock_conn.fetch.assert_not_awaited()
    mock_conn.execute.assert_not_awaited()


def test_post_rejects_empty_batch(client, mock_conn):
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": []})
    assert r.status_code == 422
    mock_conn.fetchrow.assert_not_awaited()


def test_post_rejects_oversized_batch(client, mock_conn):
    too_many = _sample_points(tracks.MAX_BATCH + 1)
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": too_many})
    assert r.status_code == 422
    mock_conn.fetchrow.assert_not_awaited()


def test_post_rejects_out_of_range_coords(client, mock_conn):
    bad = [{"recorded_at": "2026-05-09T14:00:00Z", "lat": 95.0, "lon": -87.8}]
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": bad})
    assert r.status_code == 422


def test_post_accepts_missing_speed_and_heading(client, mock_conn):
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}]
    )
    points = [{"recorded_at": "2026-05-09T14:00:00Z", "lat": 42.30, "lon": -87.80}]

    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})

    assert r.status_code == 201
    assert r.json()["inserted"] == 1
    args = mock_conn.fetch.await_args.args
    speeds, headings = args[5], args[6]
    assert speeds == [None]
    assert headings == [None]


def test_post_emits_mark_pass_when_batch_rounds_a_mark(client, mock_conn):
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    mock_conn.fetchrow.return_value = _race_row(marks=[mark])

    base = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    # Track spans ~±148m around the mark so it cleanly enters AND exits
    # the 75m FINAL_MARK_RADIUS_M zone used for single-mark courses
    # (radii_for_course(1) == [75]). The earlier ±74m geometry would
    # leave the boat inside the radius for the entire track and never
    # fire the exit transition.
    points = [
        {
            "recorded_at": (base + timedelta(seconds=i * 5)).isoformat(),
            "lat": 42.30,
            "lon": -87.80 - 0.0018 + i * 0.00045,
            "speed_kts": 5.0,
            "heading_deg": 90.0,
        }
        for i in range(9)
    ]

    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})

    assert r.status_code == 201
    body = r.json()
    assert body["inserted"] == 9
    assert len(body["new_mark_passes"]) == 1
    assert body["new_mark_passes"][0]["mark_index"] == 0
    assert body["mark_passes"] == body["new_mark_passes"]
    # After 2026-06-01 idempotency change: fetch=1 (GPS INSERT
    # RETURNING 1), execute=1 (mark-pass UPDATE only).
    assert mock_conn.fetch.await_count == 1
    assert mock_conn.execute.await_count == 1
    update_call = mock_conn.execute.await_args.args
    assert "UPDATE race_sessions" in update_call[0]
    assert "mark_passes" in update_call[0]
    # The writer passes the plain list to the ::jsonb param (codec encodes
    # once) — not a json.dumps string. Read it directly.
    persisted = update_call[1]
    assert len(persisted) == 1
    assert persisted[0]["mark_index"] == 0


def test_post_persists_detector_state_when_no_new_passes(client, mock_conn):
    """No mark passes detected → still ONE UPDATE to persist the
    detector traversal state for the next batch.

    Call topology after 2026-06-04 (migration 0020): fetch=1 (GPS
    INSERT only), execute=1 (detector_state UPDATE — no mark_passes
    column in the SQL because no new passes detected).

    Pre-0020 this test asserted ``execute.await_count == 0`` and was
    named ``test_post_skips_update_when_no_new_passes``. The
    cross-batch state persistence work makes the UPDATE unconditional;
    without it, the depart-confirm counter resets on every small batch
    and live races miss marks despite tight CPAs (see Beer Can Race 4
    post-mortem in sailline-docs/2026-06-04_session.md, Item 5).
    """
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}],
        mark_passes=[],
    )
    r = client.post(
        f"/api/races/{uuid4()}/track", json={"points": _sample_points(3)}
    )
    assert r.status_code == 201
    assert mock_conn.fetch.await_count == 1
    assert mock_conn.execute.await_count == 1
    update_sql = mock_conn.execute.await_args.args[0]
    assert "detector_state" in update_sql
    assert "mark_passes" not in update_sql


def test_post_resumes_from_existing_passes(client, mock_conn):
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
    mock_conn.fetchrow.return_value = _race_row(marks=marks, mark_passes=existing)

    base = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    points = [
        {
            "recorded_at": (base + timedelta(seconds=i * 5)).isoformat(),
            "lat": 42.30,
            "lon": -87.80 - 0.0009 + i * 0.000225,
            "speed_kts": 5.0,
            "heading_deg": 90.0,
        }
        for i in range(9)
    ]

    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})

    assert r.status_code == 201
    body = r.json()
    assert body["new_mark_passes"] == []
    assert len(body["mark_passes"]) == 1
    assert body["mark_passes"][0]["mark_index"] == 0


# --- started_at / ended_at writes (v2 — 2026-05-26) -------------------


def _finish_pass_points(
    mark_lat: float = 42.30,
    mark_lon: float = -87.80,
    base: datetime = None,
):
    """A 9-point track that cleanly enters AND exits the 75m
    ``FINAL_MARK_RADIUS_M`` zone around ``(mark_lat, mark_lon)``,
    triggering one mark pass at the closest-approach midpoint.

    Spans ~±148m (so the boat is well outside the 75m radius at both
    ends), with the closest approach right at the mark.
    """
    base = base or datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    return [
        {
            "recorded_at": (base + timedelta(seconds=i * 5)).isoformat(),
            "lat": mark_lat,
            "lon": mark_lon - 0.0018 + i * 0.00045,
            "speed_kts": 5.0,
            "heading_deg": 90.0,
        }
        for i in range(9)
    ]


def test_post_backfills_started_at_when_null(client, mock_conn):
    """First telemetry POST with a NULL started_at + populated start_at
    should write started_at = start_at in the same UPDATE that persists
    mark passes."""
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    gun = datetime(2026, 5, 14, 17, 55, tzinfo=timezone.utc)
    mock_conn.fetchrow.return_value = _race_row(
        marks=[mark], started_at=None, start_at=gun,
    )

    points = _finish_pass_points()
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})
    assert r.status_code == 201
    # After 2026-06-01: fetch=1 (INSERT track_points RETURNING 1),
    # execute=1 (the combined mark-pass + started_at UPDATE).
    assert mock_conn.fetch.await_count == 1
    assert mock_conn.execute.await_count == 1
    update_call = mock_conn.execute.await_args
    sql = update_call.args[0]
    assert "started_at = " in sql
    # start_at value should be in the args list.
    assert gun in update_call.args[1:]


def test_post_does_not_overwrite_started_at_when_already_set(client, mock_conn):
    """Subsequent batches must NOT touch started_at — it's a once-only
    write. Without the COALESCE-style guard this could overwrite a
    user-edited started_at on every flush."""
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    prior_started = datetime(2026, 5, 14, 17, 50, tzinfo=timezone.utc)
    gun = datetime(2026, 5, 14, 17, 55, tzinfo=timezone.utc)
    mock_conn.fetchrow.return_value = _race_row(
        marks=[mark], started_at=prior_started, start_at=gun,
    )

    points = _finish_pass_points()
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})
    assert r.status_code == 201
    update_call = mock_conn.execute.await_args
    sql = update_call.args[0]
    assert "started_at" not in sql


def test_post_does_not_write_started_at_when_start_at_null(client, mock_conn):
    """Race with no scheduled gun time (ad-hoc record). Nothing to
    backfill from — started_at stays NULL. The hook never errors; the
    UPDATE simply omits the column."""
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    mock_conn.fetchrow.return_value = _race_row(
        marks=[mark], started_at=None, start_at=None,
    )

    points = _finish_pass_points()
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})
    assert r.status_code == 201
    # Mark pass still fires, so there's still an UPDATE — just no
    # started_at column.
    update_call = mock_conn.execute.await_args
    sql = update_call.args[0]
    assert "started_at" not in sql


def test_post_writes_ended_at_when_final_mark_rounded(client, mock_conn):
    """The batch that closes the course should set ended_at to the
    final pass's timestamp (closest-approach to the final mark)."""
    mark = {"name": "Finish", "lat": 42.30, "lon": -87.80}
    mock_conn.fetchrow.return_value = _race_row(marks=[mark])

    points = _finish_pass_points()
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})
    assert r.status_code == 201
    update_call = mock_conn.execute.await_args
    sql = update_call.args[0]
    assert "ended_at = COALESCE(ended_at," in sql
    # The pass timestamp landed in the args.
    body = r.json()
    pass_ts_iso = body["new_mark_passes"][-1]["ts"]
    expected_ts = datetime.fromisoformat(pass_ts_iso)
    assert expected_ts in update_call.args[1:]


def test_post_does_not_write_ended_at_on_intermediate_mark(client, mock_conn):
    """Rounding a mark that's NOT the last in the course must not set
    ended_at — that would mark the race "finished" prematurely on
    multi-mark courses."""
    marks = [
        {"name": "A", "lat": 42.30, "lon": -87.80},
        {"name": "B", "lat": 42.40, "lon": -87.80},
    ]
    mock_conn.fetchrow.return_value = _race_row(marks=marks)

    # Pass through mark A only (intermediate). Mark A is at index 0 of
    # a 2-mark course, so its radius is the DEFAULT_RADIUS_M (50m), not
    # the wider final-mark zone. The original ±74m geometry is fine
    # here — it cleanly enters and exits 50m.
    base = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    points = [
        {
            "recorded_at": (base + timedelta(seconds=i * 5)).isoformat(),
            "lat": 42.30,
            "lon": -87.80 - 0.0009 + i * 0.000225,
            "speed_kts": 5.0,
            "heading_deg": 90.0,
        }
        for i in range(9)
    ]
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})
    assert r.status_code == 201
    update_call = mock_conn.execute.await_args
    sql = update_call.args[0]
    assert "ended_at" not in sql


# --- Postprocess trigger ----------------------------------------------


def test_post_triggers_postprocess_when_final_mark_rounded(
    client, mock_conn, monkeypatch
):
    """A batch that rounds the LAST mark should kick off the job."""
    fake_trigger = AsyncMock()
    monkeypatch.setattr(track_ingest, "trigger_race_postprocess", fake_trigger)

    # Single-mark course with no prior passes — this batch should
    # round it and trip the final-mark gate. Wider geometry needed
    # because a single-mark course uses the 75m FINAL_MARK_RADIUS_M.
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    mock_conn.fetchrow.return_value = _race_row(marks=[mark])

    points = _finish_pass_points()
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})
    assert r.status_code == 201
    assert fake_trigger.await_count == 1


def test_post_does_not_trigger_when_intermediate_mark(
    client, mock_conn, monkeypatch
):
    """A batch that rounds an EARLIER-but-not-final mark must not
    fire the trigger — beer-can layouts where start ~= finish would
    repeatedly trigger if we got this wrong."""
    fake_trigger = AsyncMock()
    monkeypatch.setattr(track_ingest, "trigger_race_postprocess", fake_trigger)

    # Two-mark course — boat rounds mark 0 in this batch, mark 1 is
    # nowhere near, so passes after = 1 < 2 = total marks.
    marks = [
        {"name": "A", "lat": 42.30, "lon": -87.80},
        {"name": "B", "lat": 42.40, "lon": -87.80},
    ]
    mock_conn.fetchrow.return_value = _race_row(marks=marks)

    base = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    points = [
        {
            "recorded_at": (base + timedelta(seconds=i * 5)).isoformat(),
            "lat": 42.30,
            "lon": -87.80 - 0.0009 + i * 0.000225,
            "speed_kts": 5.0,
            "heading_deg": 90.0,
        }
        for i in range(9)
    ]
    r = client.post(f"/api/races/{uuid4()}/track", json={"points": points})
    assert r.status_code == 201
    fake_trigger.assert_not_awaited()


def test_post_does_not_trigger_when_no_new_passes(
    client, mock_conn, monkeypatch
):
    """No new roundings in the batch — even on a fully-completed race
    a re-flushed batch shouldn't re-fire the job."""
    fake_trigger = AsyncMock()
    monkeypatch.setattr(track_ingest, "trigger_race_postprocess", fake_trigger)

    # Far-away mark; no points round it.
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}],
    )
    r = client.post(
        f"/api/races/{uuid4()}/track", json={"points": _sample_points(3)}
    )
    assert r.status_code == 201
    fake_trigger.assert_not_awaited()


# --- GET replay --------------------------------------------------------


def test_get_returns_chronological_track(client, mock_conn):
    race_id = uuid4()
    base = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
    rows = [
        {
            "recorded_at": base + timedelta(seconds=i),
            "lat": 42.3 + i * 0.001,
            "lon": -87.8,
            "speed_kts": 5.5,
            "heading_deg": 180.0,
        }
        for i in range(3)
    ]
    mock_conn.fetchrow.return_value = _owned()
    # GET replay reads track_points (not INSERT) — clear the
    # fixture's INSERT-aware side_effect and use return_value.
    mock_conn.fetch.side_effect = None
    mock_conn.fetch.return_value = rows

    r = client.get(f"/api/races/{race_id}/track")

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    assert body[0]["lat"] == 42.3
    assert body[2]["lat"] == 42.302

    sql = mock_conn.fetch.await_args.args[0]
    assert "ST_Y(position::geometry)" in sql
    assert "ST_X(position::geometry)" in sql
    assert "ORDER BY recorded_at ASC" in sql


def test_get_404_when_race_not_owned(client, mock_conn):
    mock_conn.fetchrow.return_value = None

    r = client.get(f"/api/races/{uuid4()}/track")

    assert r.status_code == 404
    mock_conn.fetch.assert_not_awaited()


def test_get_returns_empty_list_for_unrecorded_race(client, mock_conn):
    mock_conn.fetchrow.return_value = _owned()
    mock_conn.fetch.side_effect = None
    mock_conn.fetch.return_value = []

    r = client.get(f"/api/races/{uuid4()}/track")

    assert r.status_code == 200
    assert r.json() == []


# --- Idempotency + concurrency (2026-06-01, migration 0018) ----------


def test_post_reports_actual_landed_count(client, mock_conn):
    """ON CONFLICT skipped rows must not be counted in ``inserted``.

    A re-sent batch with some-already-stored points returns 200 with
    ``inserted`` equal to the rows that newly landed — not the rows
    sent. The durable-queue recorder uses this to decide what to drop
    from its local buffer.
    """
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}]
    )

    async def _half_landed(sql: str, *args, **kwargs):
        if "INSERT INTO track_points" in sql:
            return [{"?column?": 1}, {"?column?": 1}]
        return []

    mock_conn.fetch.side_effect = _half_landed

    r = client.post(
        f"/api/races/{uuid4()}/track",
        json={"points": _sample_points(4)},
    )

    assert r.status_code == 201
    assert r.json()["inserted"] == 2


def test_post_reports_zero_when_full_duplicate_batch(client, mock_conn):
    """A fully-duplicate re-send returns 200 + inserted=0. The recover-
    from-lost-ack path: server committed but the response never reached
    the client; the retry safely no-ops at the DB layer."""
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}]
    )

    async def _all_duplicates(sql: str, *args, **kwargs):
        if "INSERT INTO track_points" in sql:
            return []
        return []

    mock_conn.fetch.side_effect = _all_duplicates

    r = client.post(
        f"/api/races/{uuid4()}/track",
        json={"points": _sample_points(3)},
    )

    assert r.status_code == 201
    assert r.json()["inserted"] == 0


def test_post_load_uses_for_update(client, mock_conn):
    """The race-row read MUST acquire ``FOR UPDATE`` so concurrent
    batches against the same race serialize on the row lock.

    Without ``FOR UPDATE`` the read-modify-write on ``mark_passes``
    can race when the recorder's durable queue retries a batch in
    parallel with a fresh online batch — both reads see the same
    starting passes list, both compute new passes, and the second
    UPDATE overwrites the first.
    """
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}]
    )

    r = client.post(
        f"/api/races/{uuid4()}/track",
        json={"points": _sample_points(1)},
    )

    assert r.status_code == 201
    auth_sql = mock_conn.fetchrow.await_args.args[0]
    assert "FOR UPDATE" in auth_sql


def test_post_wraps_work_in_transaction(client, mock_conn):
    """The handler must open ``conn.transaction()`` so the FOR UPDATE
    lock held by ``load_race_for_ingest`` persists across the INSERT
    and the mark-pass UPDATE."""
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}]
    )

    r = client.post(
        f"/api/races/{uuid4()}/track",
        json={"points": _sample_points(1)},
    )

    assert r.status_code == 201
    # transaction() must have been called exactly once.
    assert mock_conn.transaction.call_count == 1
