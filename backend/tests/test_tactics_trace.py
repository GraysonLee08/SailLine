"""Tests for the tactician Phase A observability layer (2026-07-09).

Three surfaces:

1. ``services/tactics/trace.py`` — record shape, ring-buffer write
   (LPUSH + LTRIM + EXPIRE), never-raises contract.
2. ``services/tactics/pipeline.py`` exit instrumentation — mocked
   pool + redis, asserting the trace record each exit writes and the
   transient-exit cooldown release. This also starts paying down the
   "pipeline has no mocked-I/O test" debt flagged 2026-06-11.
3. ``routers/tactics_debug.py`` — auth 404 pattern + response
   assembly, same override style as test_boats_router.py.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, redis_client
from app.services.redis_keys import (
    TACTICS_TRACE_TTL_S,
    tactics_cooldown_key,
    tactics_trace_key,
)
from app.services.tactics import pipeline
from app.services.tactics.trace import (
    EXIT_REASONS,
    TRACE_MAX_ENTRIES,
    EvalTrace,
)

NOW = datetime(2026, 7, 9, 18, 0, 0, tzinfo=timezone.utc)
RACE_ID = UUID("11111111-2222-3333-4444-555555555555")


# ─── Fakes ───────────────────────────────────────────────────────────


class FakePipeline:
    """Records queued commands; execute() returns canned OKs."""

    def __init__(self, sink: list):
        self._sink = sink
        self._queued: list[tuple] = []

    def lpush(self, key, blob):
        self._queued.append(("lpush", key, blob))

    def ltrim(self, key, start, stop):
        self._queued.append(("ltrim", key, start, stop))

    def expire(self, key, ttl):
        self._queued.append(("expire", key, ttl))

    def lrange(self, key, start, stop):
        self._queued.append(("lrange", key, start, stop))

    def get(self, key):
        self._queued.append(("get", key))

    def ttl(self, key):
        self._queued.append(("ttl", key))

    async def execute(self):
        self._sink.extend(self._queued)
        return [1] * len(self._queued)


class FakeRedis:
    """The subset of redis.asyncio the pipeline + trace touch."""

    def __init__(self, *, setnx_result=True):
        self.commands: list[tuple] = []
        self.deleted: list[str] = []
        self.setnx_result = setnx_result

    def pipeline(self):
        return FakePipeline(self.commands)

    async def set(self, key, value, ex=None, nx=False):
        self.commands.append(("set", key, ex, nx))
        return self.setnx_result

    async def delete(self, key):
        self.deleted.append(key)
        return 1

    async def get(self, key):
        self.commands.append(("get", key))
        return None

    async def setex(self, key, ttl, value):
        self.commands.append(("setex", key, ttl))
        return True

    async def publish(self, channel, blob):
        self.commands.append(("publish", channel))
        return 1


def traces_written(fake: FakeRedis) -> list[dict]:
    return [
        json.loads(cmd[2].decode())
        for cmd in fake.commands
        if cmd[0] == "lpush"
    ]


# ─── 1. EvalTrace unit tests ─────────────────────────────────────────


async def test_trace_record_shape_and_ring_write():
    fake = FakeRedis()
    trace = EvalTrace(RACE_ID, now=NOW)
    trace.gate("track_points", 12)
    trace.set_context(
        evals=[{"actual_kts": 5.5, "target_kts": 6.0, "speed_ratio": 0.9167,
                "twa": 40.0, "tws_kts": 12.0, "vmg_ratio": 0.9}],
        heel_stat={"median_abs_deg": 21.0, "mount_ok": True},
        next_mark={"label": "SA7"},
        mark_dist_nm=0.6231,
    )
    record = await trace.finish(fake, "no_candidates")

    assert record["exit"] == "no_candidates"
    assert record["t"] == "2026-07-09T18:00:00+00:00"
    assert isinstance(record["elapsed_ms"], int)
    assert record["gates"] == {"track_points": 12}
    ctx = record["context"]
    assert ctx["sog_kts"] == 5.5 and ctx["target_kts"] == 6.0
    assert ctx["heel_median_abs_deg"] == 21.0
    assert ctx["next_mark"] == "SA7"
    assert ctx["mark_dist_nm"] == 0.62  # rounded

    ops = [c[0] for c in fake.commands]
    assert ops == ["lpush", "ltrim", "expire"]
    key = tactics_trace_key(RACE_ID)
    assert fake.commands[0][1] == key
    assert fake.commands[1] == ("ltrim", key, 0, TRACE_MAX_ENTRIES - 1)
    assert fake.commands[2] == ("expire", key, TACTICS_TRACE_TTL_S)
    # The stored blob round-trips to the returned record.
    assert traces_written(fake)[0] == record


async def test_trace_candidates_serialize_eta_and_winner():
    from app.services.tactics.detectors import CallCandidate

    fake = FakeRedis()
    trace = EvalTrace(RACE_ID, now=NOW)
    layline = CallCandidate(
        call_type="layline", call_class="maneuver",
        diagnosis={"eta_s": 240.0}, eta=NOW + timedelta(seconds=240),
    )
    off_pace = CallCandidate(
        call_type="off_pace", call_class="coaching",
        diagnosis={"speed_ratio": 0.85},
    )
    trace.set_candidates([layline, off_pace], winner=layline)
    record = await trace.finish(fake, "published")

    cands = record["candidates"]
    assert [c["type"] for c in cands] == ["layline", "off_pace"]
    assert cands[0]["won"] is True and cands[1]["won"] is False
    assert cands[0]["eta"] == "2026-07-09T18:04:00+00:00"
    assert cands[1]["diagnosis"] == {"speed_ratio": 0.85}


async def test_trace_never_raises_on_redis_failure():
    class BrokenRedis:
        def pipeline(self):
            raise ConnectionError("redis down")

    trace = EvalTrace(RACE_ID, now=NOW)
    record = await trace.finish(BrokenRedis(), "published")
    # Record still returned for the structured log line.
    assert record is not None and record["exit"] == "published"


async def test_trace_tolerates_unknown_exit_reason():
    fake = FakeRedis()
    trace = EvalTrace(RACE_ID, now=NOW)
    record = await trace.finish(fake, "not_a_real_reason")
    assert record["exit"] == "not_a_real_reason"  # logged, not lost
    assert len(traces_written(fake)) == 1


def test_exit_reasons_pinned():
    """New exits must be added here AND in trace.EXIT_REASONS."""
    assert EXIT_REASONS == {
        "cooldown_global", "race_not_found", "race_ended", "not_live",
        "opted_out", "insufficient_track", "no_forecast", "no_candidates",
        "cooldown_type", "advisor_silent_or_failed", "dropped_late",
        "published", "error",
    }


# ─── 2. Pipeline exit instrumentation (mocked I/O) ───────────────────


def _fake_pool(fetchrow_results: list, fetch_results: list):
    conn = AsyncMock()
    conn.fetchrow.side_effect = fetchrow_results
    conn.fetch.side_effect = fetch_results

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _race_row(**overrides):
    base = {
        "name": "Beer Can", "marks": [], "mark_passes": [],
        "start_at": NOW - timedelta(hours=1),
        "started_at": NOW - timedelta(hours=1),
        "ended_at": None, "mode": "race", "boat_class": "generic",
    }
    base.update(overrides)
    return base


def _track_rows(n=3):
    return [
        {"recorded_at": NOW - timedelta(seconds=30 * (n - i)),
         "lat": 41.88 + i * 1e-4, "lon": -87.60,
         "speed_kts": 5.0, "heading_deg": 180.0}
        for i in range(n)
    ]


async def test_pipeline_traces_cooldown_global(monkeypatch):
    fake = FakeRedis(setnx_result=False)  # someone else holds the window
    monkeypatch.setattr(redis_client, "get_client", lambda: fake)

    await pipeline._evaluate(RACE_ID, "u1")

    records = traces_written(fake)
    assert len(records) == 1
    assert records[0]["exit"] == "cooldown_global"


async def test_pipeline_traces_opted_out_and_keeps_cooldown(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_client", lambda: fake)
    pool = _fake_pool(
        fetchrow_results=[
            _race_row(),
            {"app_settings": {"tactician": {"enabled": False}}},
        ],
        fetch_results=[],
    )
    monkeypatch.setattr(db, "get_pool", lambda: pool)

    await pipeline._evaluate(RACE_ID, "u1")

    records = traces_written(fake)
    assert [r["exit"] for r in records] == ["opted_out"]
    # Permanent gate: the global cooldown stays burned (throttles the
    # re-check to once per window, not once per 30 s batch).
    assert fake.deleted == []


async def test_pipeline_traces_no_forecast_and_releases_cooldown(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_client", lambda: fake)
    pool = _fake_pool(
        fetchrow_results=[_race_row(), {"app_settings": {}}],
        fetch_results=[_track_rows(3), [], [], []],  # track/imu/cal/calls
    )
    monkeypatch.setattr(db, "get_pool", lambda: pool)

    async def no_forecast(track, now):
        return None

    monkeypatch.setattr(pipeline, "_load_forecast", no_forecast)

    await pipeline._evaluate(RACE_ID, "u1")

    records = traces_written(fake)
    assert [r["exit"] for r in records] == ["no_forecast"]
    assert records[0]["gates"]["track_points"] == 3
    assert records[0]["gates"]["forecast"] is False
    # Transient gate: cooldown released so the next batch can retry.
    assert fake.deleted == [tactics_cooldown_key(RACE_ID)]


async def test_pipeline_traces_error_exit(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_client", lambda: fake)

    def boom():
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(db, "get_pool", boom)

    # The safe wrapper swallows; _evaluate itself re-raises after tracing.
    await pipeline.evaluate_tactics_safe(RACE_ID, "u1")

    records = traces_written(fake)
    assert [r["exit"] for r in records] == ["error"]


# ─── 3. Debug endpoint ───────────────────────────────────────────────


@pytest.fixture
def user():
    return {"uid": "u1", "email": "t@x", "tier": "pro", "claims": {}}


def _make_app(user, mock_conn):
    from app.auth import get_current_user
    from app.routers import tactics_debug

    @asynccontextmanager
    async def fake_acquire():
        yield mock_conn

    pool = MagicMock()
    pool.acquire = fake_acquire

    app = FastAPI()
    app.include_router(tactics_debug.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


class DebugFakeRedis:
    """Pipeline-only fake for the debug endpoint's batched reads."""

    def __init__(self, *, traces, latest, global_ttl, type_ttls):
        self._responses = None
        self.traces = traces
        self.latest = latest
        self.global_ttl = global_ttl
        self.type_ttls = type_ttls

    def pipeline(self):
        outer = self

        class P:
            def __init__(self):
                self.n_ttl_calls = 0
                self.results = []

            def lrange(self, key, start, stop):
                self.results.append(outer.traces)

            def get(self, key):
                self.results.append(outer.latest)

            def ttl(self, key):
                if self.n_ttl_calls == 0:
                    self.results.append(outer.global_ttl)
                else:
                    self.results.append(
                        outer.type_ttls.get(key.rsplit(":", 1)[-1], -2)
                    )
                self.n_ttl_calls += 1

            async def execute(self):
                return self.results

        return P()


def test_debug_404_when_no_access(monkeypatch, user):
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None  # predicate filtered it out
    client = TestClient(_make_app(user, mock_conn))
    r = client.get(f"/api/races/{RACE_ID}/tactics/debug")
    assert r.status_code == 404


def test_debug_returns_traces_latest_and_cooldowns(monkeypatch, user):
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": RACE_ID}

    trace_blob = json.dumps({"exit": "no_candidates", "gates": {}}).encode()
    latest_blob = json.dumps({"type": "tactics", "message": "hold lane"})
    fake = DebugFakeRedis(
        traces=[trace_blob, b"corrupt{{{"],   # corrupt entry dropped
        latest=latest_blob,
        global_ttl=120,
        type_ttls={"layline": 300},           # others -2 (absent)
    )
    monkeypatch.setattr(redis_client, "get_client", lambda: fake)

    client = TestClient(_make_app(user, mock_conn))
    r = client.get(f"/api/races/{RACE_ID}/tactics/debug")
    assert r.status_code == 200
    body = r.json()
    assert body["traces"] == [{"exit": "no_candidates", "gates": {}}]
    assert body["latest_call"]["message"] == "hold lane"
    assert body["cooldowns"]["global_ttl_s"] == 120
    assert body["cooldowns"]["per_type"] == {"layline": 300}


def test_debug_503_when_redis_down(monkeypatch, user):
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": RACE_ID}

    def down():
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(redis_client, "get_client", down)
    client = TestClient(_make_app(user, mock_conn))
    r = client.get(f"/api/races/{RACE_ID}/tactics/debug")
    assert r.status_code == 503
