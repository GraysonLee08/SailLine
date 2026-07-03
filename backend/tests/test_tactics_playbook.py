"""Tests for app/services/tactics/playbook.py — the pre-race brief.

DB pool and Redis are faked; the forecast is a stub with .sample().
Covers: cache hit (positive + negative), match against past playbooks,
no-match caching, snapshot injection shape, and failure posture.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.services.tactics.playbook import get_playbook
from app.services.tactics.snapshot import build_snapshot
from app.services.tactics.detectors import CallCandidate

T0 = datetime(2026, 7, 1, 18, 0, 0, tzinfo=timezone.utc)
RACE_ID = uuid4()


class FakeRedis:
    def __init__(self, preload: Optional[dict] = None):
        self.store: dict[str, bytes] = {}
        if preload:
            for k, v in preload.items():
                self.store[k] = json.dumps(v).encode()
        self.setex_calls: list[tuple] = []

    async def get(self, key: str):
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: bytes):
        self.setex_calls.append((key, ttl))
        self.store[key] = value


class _FakeConn:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.queries: list[tuple] = []

    async def fetch(self, q: str, *args: Any):
        self.queries.append((q, args))
        return self._rows


class FakePool:
    def __init__(self, rows: list[dict]):
        self.conn = _FakeConn(rows)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


class FakeForecast:
    """Constant 10 kt northerly."""

    def sample(self, lat, lon, t):
        return 0.0, -5.144


def _past_race(name: str, *, tws_lo=8.0, tws_hi=12.0, twd=0.0,
               character="steady") -> dict:
    return {
        "id": uuid4(),
        "name": name,
        "playbook": {
            "signature": {
                "tws_lo_kts": tws_lo, "tws_hi_kts": tws_hi,
                "twd_mean_deg": twd, "character": character,
                "osc_amplitude_deg": None, "tws_trend": "steady",
            },
            "signature_text": f"TWS {tws_lo:.0f}-{tws_hi:.0f} kt",
            "directives": ["Tack on headers >= 8 deg", "Start at the pin"],
        },
    }


async def test_match_found_and_cached():
    redis = FakeRedis()
    pool = FakePool([_past_race("Beer Can 6.18")])
    out = await get_playbook(
        redis=redis, pool=pool, race_id=RACE_ID, uid="u1",
        forecast=FakeForecast(), lat=42.0, lon=-87.6, now=T0,
    )
    assert out is not None
    assert out["matched"] is True
    assert out["source_race_name"] == "Beer Can 6.18"
    assert out["directives"][0].startswith("Tack on headers")
    assert out["score"] >= 0.6
    # Result was cached.
    assert len(redis.setex_calls) == 1


async def test_no_match_returns_none_and_caches_negative():
    redis = FakeRedis()
    # Past race in heavy air from the south — today is 10 kt northerly.
    pool = FakePool([_past_race(
        "Storm race", tws_lo=22.0, tws_hi=28.0, twd=180.0,
        character="persistent_right",
    )])
    out = await get_playbook(
        redis=redis, pool=pool, race_id=RACE_ID, uid="u1",
        forecast=FakeForecast(), lat=42.0, lon=-87.6, now=T0,
    )
    assert out is None
    # Negative result cached too — no re-query per telemetry batch.
    assert len(redis.setex_calls) == 1
    cached = json.loads(redis.store[f"tactics:playbook:{RACE_ID}"])
    assert cached["matched"] is False


async def test_cache_hit_skips_db():
    key = f"tactics:playbook:{RACE_ID}"
    redis = FakeRedis(preload={key: {
        "matched": True, "score": 0.9, "signature_text": "x",
        "source_race_name": "R", "directives": ["d"],
    }})
    pool = FakePool([])
    out = await get_playbook(
        redis=redis, pool=pool, race_id=RACE_ID, uid="u1",
        forecast=FakeForecast(), lat=42.0, lon=-87.6, now=T0,
    )
    assert out is not None and out["directives"] == ["d"]
    assert pool.conn.queries == []  # DB never touched


async def test_negative_cache_hit_returns_none():
    key = f"tactics:playbook:{RACE_ID}"
    redis = FakeRedis(preload={key: {"matched": False}})
    pool = FakePool([_past_race("would match")])
    out = await get_playbook(
        redis=redis, pool=pool, race_id=RACE_ID, uid="u1",
        forecast=FakeForecast(), lat=42.0, lon=-87.6, now=T0,
    )
    assert out is None
    assert pool.conn.queries == []


async def test_db_failure_degrades_to_no_match():
    class BoomPool:
        def acquire(self):
            raise RuntimeError("pool down")

    out = await get_playbook(
        redis=FakeRedis(), pool=BoomPool(), race_id=RACE_ID, uid="u1",
        forecast=FakeForecast(), lat=42.0, lon=-87.6, now=T0,
    )
    assert out is None


async def test_double_encoded_playbook_tolerated():
    """Legacy double-encoded JSONB: playbook arrives as a JSON string."""
    row = _past_race("Encoded race")
    row["playbook"] = json.dumps(row["playbook"])
    out = await get_playbook(
        redis=FakeRedis(), pool=FakePool([row]), race_id=RACE_ID, uid="u1",
        forecast=FakeForecast(), lat=42.0, lon=-87.6, now=T0,
    )
    assert out is not None and out["matched"] is True


# ─── snapshot injection ───────────────────────────────────────────────


def _candidate() -> CallCandidate:
    return CallCandidate(
        call_type="forecast_shift", call_class="maneuver",
        diagnosis={"shift_deg": 14}, eta=None,
    )


def test_snapshot_includes_playbook_block():
    snap = build_snapshot(
        candidate=_candidate(), other_candidates=[],
        race_meta={"race_name": "r"}, track=[], evals=[], forecast=None,
        next_mark=None, heel_stat=None, recent_calls=[], now=T0,
        playbook={
            "signature_text": "TWS 8-12 kt", "source_race_name": "R1",
            "score": 0.8, "directives": ["Tack on headers >= 8 deg"],
        },
    )
    assert snap["playbook"]["conditions"] == "TWS 8-12 kt"
    assert snap["playbook"]["from_race"] == "R1"
    assert snap["playbook"]["directives"] == ["Tack on headers >= 8 deg"]


def test_snapshot_omits_playbook_when_absent_or_empty():
    base = dict(
        candidate=_candidate(), other_candidates=[],
        race_meta={"race_name": "r"}, track=[], evals=[], forecast=None,
        next_mark=None, heel_stat=None, recent_calls=[], now=T0,
    )
    assert "playbook" not in build_snapshot(**base)
    assert "playbook" not in build_snapshot(
        **base, playbook={"directives": []},
    )
