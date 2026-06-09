"""Tests for app/services/track_ingest.py — the shared GPS-ingest
side-effect helper used by both ``/track`` and ``/telemetry``.

Router-level tests in ``test_tracks_router.py`` and ``test_telemetry.py``
already exercise these helpers through the wire path. This file
provides direct unit tests so the helpers can be refactored
independently of the routers and so regressions surface at the
narrowest possible scope.

Mocking pattern: asyncpg connection is a ``MagicMock`` with
``fetchrow`` and ``execute`` exposed as ``AsyncMock``s. No real DB
contact.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import track_ingest
from app.services.mark_rounding import Point as DetectorPoint


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def conn() -> MagicMock:
    c = MagicMock()
    c.fetchrow = AsyncMock()
    c.execute = AsyncMock()
    return c


def _rounding_points(mark_lat: float, mark_lon: float, n: int = 9):
    """Detector points that enter and exit the rounding radius.

    Originally tuned for the 50m radius. After 2026-05-26 the
    single-mark "final-mark" course gets `FINAL_MARK_RADIUS_M = 75m`
    via `radii_for_course(1)`, so the span was widened to ±150m: well
    outside the 75m zone at the endpoints, on the mark at the middle.
    Still works for the 50m intermediate-mark radius (it just enters
    sooner).
    """
    base = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    span = 0.0018  # ~150m at 42° lat — clears the 75m final-mark radius
    step = (2 * span) / (n - 1)
    return [
        DetectorPoint(
            lat=mark_lat,
            lon=mark_lon - span + i * step,
            ts=base + timedelta(seconds=i * 5),
        )
        for i in range(n)
    ]


# ─── load_race_for_ingest ─────────────────────────────────────────────


async def test_load_race_for_ingest_returns_parsed_jsonb(conn):
    """JSONB as Python objects (the default asyncpg codec path)
    flows through unchanged."""
    marks = [{"name": "M", "lat": 42.3, "lon": -87.8}]
    passes = [{"mark_index": 0, "ts": "2026-05-14T17:55:00+00:00",
               "lat": 42.3, "lon": -87.8}]
    conn.fetchrow.return_value = {
        "marks": marks,
        "mark_passes": passes,
        "started_at": None,
        "start_at": None,
        "mode": "distance",
        "detector_state": None,
    }

    out = await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    assert out["marks"] == marks
    assert out["mark_passes"] == passes
    assert out["detector_state"] is None


async def test_load_race_for_ingest_parses_string_jsonb(conn):
    """Defensive path: some fixtures (and older asyncpg configs)
    return JSONB as a string. The loader must parse it. Same path
    is exercised for ``detector_state`` (also a JSONB column)."""
    marks = [{"name": "M", "lat": 42.3, "lon": -87.8}]
    state = {"last_dist": 50.0, "min_dist": 12.5, "min_ts": None,
             "min_lat": 42.3, "min_lon": -87.8, "departing": 1}
    conn.fetchrow.return_value = {
        "marks": json.dumps(marks),
        "mark_passes": json.dumps([]),
        "started_at": None,
        "start_at": None,
        "mode": "distance",
        "detector_state": json.dumps(state),
    }

    out = await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    assert out["marks"] == marks
    assert out["mark_passes"] == []
    assert out["detector_state"] == state


async def test_load_race_for_ingest_handles_null_jsonb(conn):
    """A pre-Alembic race row with NULL marks/mark_passes must not
    crash. Returns empty lists. detector_state stays None — that's
    the "fresh traversal" sentinel."""
    conn.fetchrow.return_value = {
        "marks": None,
        "mark_passes": None,
        "started_at": None,
        "start_at": None,
        "mode": "distance",
        "detector_state": None,
    }

    out = await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    assert out["marks"] == []
    assert out["mark_passes"] == []
    assert out["detector_state"] is None


async def test_load_race_for_ingest_404_when_not_writeable(conn):
    """Auth predicate doesn't match → fetchrow returns None →
    HTTPException(404). Loader does not 403 (would leak existence)."""
    conn.fetchrow.return_value = None

    with pytest.raises(HTTPException) as ei:
        await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")
    assert ei.value.status_code == 404


async def test_load_race_for_ingest_uses_write_predicate(conn):
    """The loader must hit ``race_write_predicate`` shape, not the
    pre-D3 owner-only check. Asserts on the SQL we issue."""
    conn.fetchrow.return_value = {
        "marks": [],
        "mark_passes": [],
        "started_at": None,
        "start_at": None,
        "mode": "distance",
        "detector_state": None,
    }

    await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    sql = conn.fetchrow.await_args.args[0]
    assert "boat_crew" in sql
    assert "bc.role IN ('owner', 'crew')" in sql


async def test_load_race_for_ingest_acquires_for_update(conn):
    """The loader MUST request ``FOR UPDATE`` so concurrent batches
    against the same race serialize on the row lock.

    Regression guard for the 2026-06-01 concurrency fix. Without the
    lock, two overlapping batches (common once the recorder's durable
    queue retries are landing in parallel with fresh online batches)
    can read the same starting ``mark_passes`` and the second UPDATE
    overwrites the first.
    """
    conn.fetchrow.return_value = {
        "marks": [],
        "mark_passes": [],
        "started_at": None,
        "start_at": None,
        "mode": "distance",
        "detector_state": None,
    }

    await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    sql = conn.fetchrow.await_args.args[0]
    assert "FOR UPDATE" in sql


async def test_load_race_for_ingest_reads_detector_state_column(conn):
    """0020 regression: the SELECT must include ``detector_state`` so
    the cross-batch traversal-state persistence has something to
    restore. If this column is dropped the depart-confirm counter
    silently resets on every batch and Mark-2-style misses come back."""
    conn.fetchrow.return_value = {
        "marks": [],
        "mark_passes": [],
        "started_at": None,
        "start_at": None,
        "mode": "distance",
        "detector_state": None,
    }

    await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    sql = conn.fetchrow.await_args.args[0]
    assert "detector_state" in sql


# ─── detect_and_persist_new_passes ────────────────────────────────────


async def test_detect_and_persist_emits_passes(conn):
    """Happy path: batch crosses a mark → returns new pass + persists
    mark_passes AND detector_state via a single UPDATE."""
    race_id = uuid4()
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}

    all_p, new_p = await track_ingest.detect_and_persist_new_passes(
        conn,
        race_id=race_id,
        marks=[mark],
        existing_passes=[],
        new_points=_rounding_points(mark["lat"], mark["lon"]),
    )

    assert len(new_p) == 1
    assert new_p[0]["mark_index"] == 0
    assert all_p == new_p

    conn.execute.assert_awaited_once()
    sql = conn.execute.await_args.args[0]
    assert "UPDATE race_sessions" in sql
    assert "mark_passes" in sql
    # detector_state is part of the same UPDATE; emit case resets it to
    # NULL because _reset_traversal_state ran inside feed().
    assert "detector_state" in sql
    # Writers now pass the plain Python object to the ::jsonb param (the
    # asyncpg codec encodes once) — NOT a json.dumps string. So the arg is
    # the list itself, not a string to re-parse.
    persisted_passes = conn.execute.await_args.args[1]
    assert persisted_passes == new_p
    # The second positional arg is the new detector_state JSONB. After
    # an emit the state should be None (the traversal is done for this
    # mark; next batch starts fresh on the next mark).
    persisted_state = conn.execute.await_args.args[2]
    assert persisted_state is None


async def test_detect_and_persist_persists_state_when_no_passes(conn):
    """Even when no pass emits, the UPDATE MUST run to persist the
    traversal state for the next batch. Pre-0020 this case ran no
    UPDATE; post-0020 it always does, because skipping the write loses
    the depart-confirm counter that makes 1-sample-per-batch detection
    work."""
    # Place a mark *near* the point trajectory so the detector picks
    # up a traversal (last_dist non-None) without crossing the threshold
    # — gives us a non-None state to persist.
    far_mark = {"name": "Far", "lat": 42.30, "lon": -87.85}

    all_p, new_p = await track_ingest.detect_and_persist_new_passes(
        conn,
        race_id=uuid4(),
        marks=[far_mark],
        existing_passes=[],
        new_points=_rounding_points(42.30, -87.80),
    )

    assert new_p == []
    assert all_p == []
    # Single UPDATE was issued to persist the traversal state.
    conn.execute.assert_awaited_once()
    sql = conn.execute.await_args.args[0]
    assert "detector_state" in sql
    # mark_passes column is NOT in the SQL when there are no new passes
    # — the quiet-state writer only touches detector_state + updated_at
    # (+ optional started_at backfill).
    assert "mark_passes" not in sql


async def test_detect_and_persist_resumes_from_existing(conn):
    """A re-flushed batch (offline-queue retry) that re-rounds an
    already-recorded mark must NOT create a duplicate pass. State is
    still persisted (the next batch needs it)."""
    marks = [
        {"name": "A", "lat": 42.30, "lon": -87.80},
        {"name": "B", "lat": 42.31, "lon": -87.80},
    ]
    existing = [{
        "mark_index": 0,
        "ts": "2026-05-14T17:55:00+00:00",
        "lat": 42.30,
        "lon": -87.80,
    }]

    all_p, new_p = await track_ingest.detect_and_persist_new_passes(
        conn,
        race_id=uuid4(),
        marks=marks,
        existing_passes=existing,
        new_points=_rounding_points(marks[0]["lat"], marks[0]["lon"]),
    )

    assert new_p == []
    assert all_p == existing
    # Quiet-state UPDATE still runs to persist whatever traversal state
    # accumulated against mark B (the next-expected). Mark A pass is
    # NOT re-emitted (existing_passes already covers it).
    conn.execute.assert_awaited_once()


async def test_detect_and_persist_threads_state_into_detector(conn):
    """A non-None detector_state passed in MUST be restored on the
    detector, so the next batch resumes where the previous left off.

    Approach: pre-seed traversal state where ``departing=2`` and the
    running minimum is well within threshold. Feeding even a single
    further-away sample should now satisfy depart-confirm and emit.
    Without state restoration the same single sample would not emit.
    """
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    # State that represents: mark CPA was 5 m, last sample was 10 m,
    # departing count already at 2. Next strictly-increasing sample
    # tips depart_confirm to 3 → emit.
    seeded_state = {
        "last_dist": 10.0,
        "min_dist": 5.0,
        "min_ts": "2026-05-14T18:00:05+00:00",
        "min_lat": mark["lat"],
        "min_lon": mark["lon"],
        "departing": 2,
    }
    # One sample, further away than last_dist → triggers the emit.
    further = DetectorPoint(
        lat=mark["lat"] + 0.0005,  # ~55 m N
        lon=mark["lon"],
        ts=datetime(2026, 5, 14, 18, 0, 10, tzinfo=timezone.utc),
    )

    all_p, new_p = await track_ingest.detect_and_persist_new_passes(
        conn,
        race_id=uuid4(),
        marks=[mark],
        existing_passes=[],
        new_points=[further],
        detector_state=seeded_state,
    )

    assert len(new_p) == 1, (
        "With seeded state at departing=2, one more increasing sample "
        "must complete the depart-confirm and emit."
    )
    # The pass is emitted at the SEEDED min_ts (CPA from a previous batch),
    # not the timestamp of the further-away sample fed in this call.
    assert new_p[0]["ts"] == "2026-05-14T18:00:05+00:00"


async def test_detect_and_persist_skips_when_marks_malformed(conn):
    """Defensive: malformed mark dict (missing lat/lon) → bail out
    cleanly, no passes emitted, no UPDATE. The malformed-marks path
    short-circuits BEFORE the cross-batch persistence machinery, so
    no state UPDATE either."""
    all_p, new_p = await track_ingest.detect_and_persist_new_passes(
        conn,
        race_id=uuid4(),
        marks=[{"name": "Broken"}],  # no lat/lon
        existing_passes=[],
        new_points=_rounding_points(42.30, -87.80),
    )

    assert new_p == []
    assert all_p == []
    conn.execute.assert_not_called()


async def test_detect_and_persist_no_op_when_all_marks_rounded(conn):
    """If existing_passes already covers every mark, the detector
    can't emit anything new — short-circuit before constructing it.
    Still issues an UPDATE to clear any lingering detector_state
    (the course is closed, no more traversal to track)."""
    marks = [{"name": "M", "lat": 42.30, "lon": -87.80}]
    existing = [{
        "mark_index": 0,
        "ts": "2026-05-14T17:55:00+00:00",
        "lat": 42.30,
        "lon": -87.80,
    }]

    all_p, new_p = await track_ingest.detect_and_persist_new_passes(
        conn,
        race_id=uuid4(),
        marks=marks,
        existing_passes=existing,
        new_points=_rounding_points(marks[0]["lat"], marks[0]["lon"]),
    )

    assert new_p == []
    assert all_p == existing
    # Single UPDATE to clear detector_state to NULL.
    conn.execute.assert_awaited_once()
    sql = conn.execute.await_args.args[0]
    assert "detector_state" in sql
    state_val = conn.execute.await_args.args[1]
    assert state_val is None


# ─── maybe_trigger_postprocess ────────────────────────────────────────


async def test_trigger_fires_at_final_mark(monkeypatch):
    """All marks now passed AND this batch produced at least one new
    pass → trigger fires."""
    fake = AsyncMock()
    monkeypatch.setattr(track_ingest, "trigger_race_postprocess", fake)

    fired = await track_ingest.maybe_trigger_postprocess(
        race_id=uuid4(),
        marks=[{"name": "M", "lat": 42.30, "lon": -87.80}],
        all_passes=[{"mark_index": 0, "ts": "x", "lat": 0, "lon": 0}],
        new_passes=[{"mark_index": 0, "ts": "x", "lat": 0, "lon": 0}],
    )

    assert fired is True
    fake.assert_awaited_once()


async def test_trigger_skips_when_no_new_passes(monkeypatch):
    """All marks passed already but this batch produced nothing new —
    re-flush of a completed race. Must not re-fire."""
    fake = AsyncMock()
    monkeypatch.setattr(track_ingest, "trigger_race_postprocess", fake)

    fired = await track_ingest.maybe_trigger_postprocess(
        race_id=uuid4(),
        marks=[{"name": "M", "lat": 42.30, "lon": -87.80}],
        all_passes=[{"mark_index": 0, "ts": "x", "lat": 0, "lon": 0}],
        new_passes=[],
    )

    assert fired is False
    fake.assert_not_awaited()


async def test_trigger_skips_intermediate_mark(monkeypatch):
    """Two-mark course, only mark 0 rounded — must not fire."""
    fake = AsyncMock()
    monkeypatch.setattr(track_ingest, "trigger_race_postprocess", fake)

    fired = await track_ingest.maybe_trigger_postprocess(
        race_id=uuid4(),
        marks=[
            {"name": "A", "lat": 42.30, "lon": -87.80},
            {"name": "B", "lat": 42.40, "lon": -87.80},
        ],
        all_passes=[{"mark_index": 0, "ts": "x", "lat": 0, "lon": 0}],
        new_passes=[{"mark_index": 0, "ts": "x", "lat": 0, "lon": 0}],
    )

    assert fired is False
    fake.assert_not_awaited()


async def test_trigger_skips_when_zero_marks(monkeypatch):
    """Defensive — a race with no marks at all must not trigger
    (would otherwise treat 0 == 0 as 'all rounded')."""
    fake = AsyncMock()
    monkeypatch.setattr(track_ingest, "trigger_race_postprocess", fake)

    fired = await track_ingest.maybe_trigger_postprocess(
        race_id=uuid4(),
        marks=[],
        all_passes=[],
        new_passes=[{"mark_index": 0, "ts": "x", "lat": 0, "lon": 0}],
    )

    assert fired is False
    fake.assert_not_awaited()
