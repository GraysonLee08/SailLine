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
    return AsyncMock()


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


def _race_row(marks=None, mark_passes=None):
    if marks is None:
        marks = [{"name": "M", "lat": 42.30, "lon": -87.80}]
    if mark_passes is None:
        mark_passes = []
    return {"marks": json.dumps(marks), "mark_passes": json.dumps(mark_passes)}


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
    mock_conn.execute.assert_awaited_once()
    args = mock_conn.execute.await_args.args
    sql = args[0]
    assert "INSERT INTO track_points" in sql
    assert "unnest" in sql.lower()
    assert "ST_SetSRID(ST_MakePoint" in sql
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
    args = mock_conn.execute.await_args.args
    speeds, headings = args[5], args[6]
    assert speeds == [None]
    assert headings == [None]


def test_post_emits_mark_pass_when_batch_rounds_a_mark(client, mock_conn):
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    mock_conn.fetchrow.return_value = _race_row(marks=[mark])

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
    assert body["inserted"] == 9
    assert len(body["new_mark_passes"]) == 1
    assert body["new_mark_passes"][0]["mark_index"] == 0
    assert body["mark_passes"] == body["new_mark_passes"]
    assert mock_conn.execute.await_count == 2
    update_call = mock_conn.execute.await_args_list[1].args
    assert "UPDATE race_sessions" in update_call[0]
    assert "mark_passes" in update_call[0]
    persisted = json.loads(update_call[1])
    assert len(persisted) == 1
    assert persisted[0]["mark_index"] == 0


def test_post_skips_update_when_no_new_passes(client, mock_conn):
    mock_conn.fetchrow.return_value = _race_row(
        marks=[{"name": "Far", "lat": 0.0, "lon": 0.0}],
        mark_passes=[],
    )
    r = client.post(
        f"/api/races/{uuid4()}/track", json={"points": _sample_points(3)}
    )
    assert r.status_code == 201
    assert mock_conn.execute.await_count == 1


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


# --- Postprocess trigger ----------------------------------------------


def test_post_triggers_postprocess_when_final_mark_rounded(
    client, mock_conn, monkeypatch
):
    """A batch that rounds the LAST mark should kick off the job."""
    fake_trigger = AsyncMock()
    monkeypatch.setattr(track_ingest, "trigger_race_postprocess", fake_trigger)

    # Single-mark course with no prior passes — this batch should
    # round it and trip the final-mark gate.
    mark = {"name": "M", "lat": 42.30, "lon": -87.80}
    mock_conn.fetchrow.return_value = _race_row(marks=[mark])

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
    mock_conn.fetch.return_value = []

    r = client.get(f"/api/races/{uuid4()}/track")

    assert r.status_code == 200
    assert r.json() == []
