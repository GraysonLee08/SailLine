"""Tests for workers/recorder_watchdog.py.

Split per the house pattern:

  * pure-function tests for ``should_notify`` (the notify-policy matrix
    — first silence / continuing silence / resumed-then-silent /
    cooldown / corrupt state), and
  * mocked-I/O orchestration tests for ``sweep`` (AsyncMock pool +
    fake Redis + monkeypatched ``send_to_user``), covering the
    dry-run, redis-down, no-owner, dedup, and state-write paths.

No real DB, Redis, or FCM anywhere. The SQL itself is pinned only
loosely (predicate fragments), matching how test_tracks_router pins
statement shape without becoming a formatting test.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from workers import recorder_watchdog as wd


NOW = datetime(2026, 7, 5, 18, 0, 0, tzinfo=timezone.utc)


def _state(last_fix_at: datetime, notified_at: datetime) -> str:
    return json.dumps(
        {
            "last_fix_at": last_fix_at.isoformat(),
            "notified_at": notified_at.isoformat(),
        }
    )


# ─── should_notify (pure) ────────────────────────────────────────────


def test_notifies_on_first_silence():
    assert wd.should_notify(None, NOW - timedelta(minutes=6), NOW) is True


def test_suppresses_repeat_within_cooldown():
    last_fix = NOW - timedelta(minutes=6)
    state = _state(last_fix, NOW - timedelta(minutes=4))
    assert wd.should_notify(state, last_fix, NOW) is False


def test_renotifies_after_cooldown():
    last_fix = NOW - timedelta(minutes=20)
    state = _state(last_fix, NOW - timedelta(minutes=16))
    assert wd.should_notify(state, last_fix, NOW) is True


def test_renotifies_immediately_when_fixes_resumed_then_died():
    """A NEWER last_fix than the state recorded means the recorder came
    back and died again — a fresh failure, no cooldown applies."""
    old_fix = NOW - timedelta(minutes=30)
    new_fix = NOW - timedelta(minutes=6)
    state = _state(old_fix, NOW - timedelta(minutes=2))  # notified recently
    assert wd.should_notify(state, new_fix, NOW) is True


@pytest.mark.parametrize(
    "bad_state",
    ["", "not json", "{}", json.dumps({"last_fix_at": "garbage"})],
)
def test_corrupt_state_treated_as_absent(bad_state):
    assert wd.should_notify(bad_state, NOW - timedelta(minutes=6), NOW) is True


def test_custom_cooldown_respected():
    last_fix = NOW - timedelta(minutes=10)
    state = _state(last_fix, NOW - timedelta(minutes=5))
    assert wd.should_notify(state, last_fix, NOW, renotify_minutes=4) is True
    assert wd.should_notify(state, last_fix, NOW, renotify_minutes=6) is False


# ─── sweep (mocked I/O) ──────────────────────────────────────────────


class FakeRedis:
    """get/set with an ex kwarg — the only surface sweep touches."""

    def __init__(self, initial: dict[str, str] | None = None):
        self.store = dict(initial or {})
        self.set_calls: list[tuple[str, str, int]] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.set_calls.append((key, value, ex))


def _make_pool(rows):
    conn = AsyncMock()
    conn.fetch.return_value = rows
    pool = MagicMock()

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool.acquire = fake_acquire
    return pool, conn


def _row(race_id, name, user_id, last_fix_at):
    return {
        "id": race_id,
        "name": name,
        "user_id": user_id,
        "last_fix_at": last_fix_at,
    }


@pytest.fixture
def push_spy(monkeypatch):
    spy = AsyncMock(return_value=1)
    monkeypatch.setattr(wd, "send_to_user", spy)
    return spy


async def test_no_rows_is_clean_exit(push_spy):
    pool, conn = _make_pool([])
    rc = await wd.sweep(pool, FakeRedis(), 5.0, 3.0, dry_run=False)
    assert rc == 0
    push_spy.assert_not_awaited()
    # Predicate fragments: open race, silence window bounded both ends.
    sql = conn.fetch.await_args.args[0]
    assert "started_at IS NOT NULL" in sql
    assert "ended_at IS NULL" in sql
    assert sql.count("make_interval") == 2


async def test_pushes_and_records_state_for_silent_race(push_spy):
    rid = uuid4()
    last_fix = datetime.now(timezone.utc) - timedelta(minutes=7)
    pool, _ = _make_pool([_row(rid, "Beer Can", "u1", last_fix)])
    redis = FakeRedis()

    rc = await wd.sweep(pool, redis, 5.0, 3.0, dry_run=False)

    assert rc == 0
    push_spy.assert_awaited_once()
    args, kwargs = push_spy.await_args
    assert args[1] == "u1"
    assert kwargs["data"] == {"kind": "deadRecorder", "raceId": str(rid)}
    assert "Beer Can" in kwargs["body"]
    # State written with the TTL from redis_keys.
    (key, value, ex), = redis.set_calls
    assert key == f"watchdog:recorder:{rid}"
    assert ex == wd.RECORDER_WATCHDOG_TTL_S
    stored = json.loads(value)
    assert datetime.fromisoformat(stored["last_fix_at"]) == last_fix


async def test_dedups_when_already_notified(push_spy):
    rid = uuid4()
    last_fix = datetime.now(timezone.utc) - timedelta(minutes=7)
    state = _state(last_fix, datetime.now(timezone.utc) - timedelta(minutes=2))
    pool, _ = _make_pool([_row(rid, "Beer Can", "u1", last_fix)])
    redis = FakeRedis({f"watchdog:recorder:{rid}": state})

    rc = await wd.sweep(pool, redis, 5.0, 3.0, dry_run=False)

    assert rc == 0
    push_spy.assert_not_awaited()
    assert redis.set_calls == []  # state untouched


async def test_dry_run_sends_and_writes_nothing(push_spy):
    rid = uuid4()
    last_fix = datetime.now(timezone.utc) - timedelta(minutes=7)
    pool, _ = _make_pool([_row(rid, "Beer Can", "u1", last_fix)])
    redis = FakeRedis()

    rc = await wd.sweep(pool, redis, 5.0, 3.0, dry_run=True)

    assert rc == 0
    push_spy.assert_not_awaited()
    assert redis.set_calls == []


async def test_redis_down_skips_sends(push_spy):
    """No dedup state → no pushes. A 2-min cadence without dedup would
    spam a notification per tick; silence is the documented failure
    mode (race_sweep still backstops the race itself)."""
    rid = uuid4()
    last_fix = datetime.now(timezone.utc) - timedelta(minutes=7)
    pool, _ = _make_pool([_row(rid, "Beer Can", "u1", last_fix)])

    rc = await wd.sweep(pool, None, 5.0, 3.0, dry_run=False)

    assert rc == 0
    push_spy.assert_not_awaited()


async def test_ownerless_race_skipped(push_spy):
    rid = uuid4()
    last_fix = datetime.now(timezone.utc) - timedelta(minutes=7)
    pool, _ = _make_pool([_row(rid, "Beer Can", None, last_fix)])
    redis = FakeRedis()

    rc = await wd.sweep(pool, redis, 5.0, 3.0, dry_run=False)

    assert rc == 0
    push_spy.assert_not_awaited()
    assert redis.set_calls == []


async def test_state_written_even_when_zero_devices(push_spy):
    """delivered == 0 must still record the notify — re-pushing into
    the void every 2 minutes burns quota for nothing; the RENOTIFY
    window retries naturally."""
    push_spy.return_value = 0
    rid = uuid4()
    last_fix = datetime.now(timezone.utc) - timedelta(minutes=7)
    pool, _ = _make_pool([_row(rid, "Beer Can", "u1", last_fix)])
    redis = FakeRedis()

    rc = await wd.sweep(pool, redis, 5.0, 3.0, dry_run=False)

    assert rc == 0
    push_spy.assert_awaited_once()
    assert len(redis.set_calls) == 1
