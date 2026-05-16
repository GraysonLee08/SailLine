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
    """Detector points that enter and exit a 50m radius around the mark."""
    base = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    return [
        DetectorPoint(
            lat=mark_lat,
            lon=mark_lon - 0.0009 + i * 0.000225,
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
    conn.fetchrow.return_value = {"marks": marks, "mark_passes": passes}

    out = await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    assert out["marks"] == marks
    assert out["mark_passes"] == passes


async def test_load_race_for_ingest_parses_string_jsonb(conn):
    """Defensive path: some fixtures (and older asyncpg configs)
    return JSONB as a string. The loader must parse it."""
    marks = [{"name": "M", "lat": 42.3, "lon": -87.8}]
    conn.fetchrow.return_value = {
        "marks": json.dumps(marks),
        "mark_passes": json.dumps([]),
    }

    out = await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    assert out["marks"] == marks
    assert out["mark_passes"] == []


async def test_load_race_for_ingest_handles_null_jsonb(conn):
    """A pre-Alembic race row with NULL marks/mark_passes must not
    crash. Returns empty lists."""
    conn.fetchrow.return_value = {"marks": None, "mark_passes": None}

    out = await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    assert out["marks"] == []
    assert out["mark_passes"] == []


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
    conn.fetchrow.return_value = {"marks": [], "mark_passes": []}

    await track_ingest.load_race_for_ingest(conn, uuid4(), "uid")

    sql = conn.fetchrow.await_args.args[0]
    assert "boat_crew" in sql
    assert "bc.role IN ('owner', 'crew')" in sql


# ─── detect_and_persist_new_passes ────────────────────────────────────


async def test_detect_and_persist_emits_passes(conn):
    """Happy path: batch crosses a mark → returns new pass + persists
    via UPDATE."""
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
    persisted = json.loads(conn.execute.await_args.args[1])
    assert persisted == new_p


async def test_detect_and_persist_no_update_when_no_passes(conn):
    """Batch doesn't cross any mark → no UPDATE call."""
    far_mark = {"name": "Far", "lat": 0.0, "lon": 0.0}

    all_p, new_p = await track_ingest.detect_and_persist_new_passes(
        conn,
        race_id=uuid4(),
        marks=[far_mark],
        existing_passes=[],
        new_points=_rounding_points(42.30, -87.80),
    )

    assert new_p == []
    assert all_p == []
    conn.execute.assert_not_called()


async def test_detect_and_persist_resumes_from_existing(conn):
    """A re-flushed batch (offline-queue retry) that re-rounds an
    already-recorded mark must NOT create a duplicate pass."""
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
    conn.execute.assert_not_called()


async def test_detect_and_persist_skips_when_marks_malformed(conn):
    """Defensive: malformed mark dict (missing lat/lon) → bail out
    cleanly, no passes emitted, no UPDATE."""
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
    can't emit anything new — short-circuit before constructing it."""
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
    conn.execute.assert_not_called()


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
