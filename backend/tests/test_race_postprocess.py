"""Tests for workers/race_postprocess.py — the orchestrator.

The DB, Redis, and Anthropic are all mocked. We exercise the decision
branches of ``process_race``:

  * race not found / no track  → no UPDATE, exit 0
  * summary current + snapshot present + no force → no work
  * --force → both regenerate even when current
  * summary stale → AI call runs; snapshot kept if already present
  * snapshot missing → wind build runs
  * generate_summary returns None → row remains with old summary
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

import pytest

from workers import race_postprocess
from app.services.race_summary import PROMPT_VERSION


T0 = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
RACE_ID = uuid4()


# ─── Stubs ────────────────────────────────────────────────────────────


def _make_track_rows(n: int = 60) -> list[dict]:
    """n points 1s apart, ~5 m/s eastward — a sane sailing track."""
    return [
        {
            "recorded_at": T0 + timedelta(seconds=i),
            "lat": 42.05,
            "lon": -87.75 + i * 0.00005,   # ~5m east per step
            "speed_kts": 10.0,
            "heading_deg": 90.0,
        }
        for i in range(n)
    ]


def _make_race_row(
    *,
    ai_summary: Optional[dict] = None,
    wind_snapshot: Optional[dict] = None,
) -> dict:
    return {
        "id": RACE_ID,
        "user_id": "uid",
        "name": "Test race",
        "boat_class": "J/70",
        "start_at": T0,
        "marks": [
            {"lat": 42.05, "lon": -87.74, "name": "A"},
            {"lat": 42.05, "lon": -87.73, "name": "B"},
        ],
        "mark_passes": [
            {
                "mark_index": 0,
                "ts": (T0 + timedelta(seconds=20)).isoformat(),
                "lat": 42.05, "lon": -87.74,
            },
            {
                "mark_index": 1,
                "ts": (T0 + timedelta(seconds=50)).isoformat(),
                "lat": 42.05, "lon": -87.73,
            },
        ],
        "ai_summary": ai_summary,
        "wind_snapshot": wind_snapshot,
        # D2 columns
        "mode": "inshore",
        "uses_spinnaker": True,
        "boat_id": None,
        "boat_hcp": None, "boat_dhcp": None,
        "boat_nshcp": None, "boat_dnshcp": None,
    }


class FakePool:
    """Sentinel — process_race never touches the pool directly in our
    tests because we monkeypatch the I/O helpers."""


class _Spy:
    def __init__(self) -> None:
        self.persist_calls: list[dict] = []
        self.wind_calls: int = 0
        self.summary_calls: int = 0


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch):
    s = _Spy()

    async def fake_persist(pool, race_id, *, ai_summary, wind_snapshot):
        s.persist_calls.append(
            {"ai_summary": ai_summary, "wind_snapshot": wind_snapshot}
        )

    async def fake_build_wind_snapshot(**kwargs: Any):
        s.wind_calls += 1
        return {"fake": "snapshot"}

    def fake_generate_summary(**kwargs: Any):
        s.summary_calls += 1
        return {
            "recap": "ok",
            "tips": [],
            "model": "test",
            "prompt_version": PROMPT_VERSION,
            "generated_at": T0.isoformat(),
        }

    monkeypatch.setattr(race_postprocess, "_persist", fake_persist)
    monkeypatch.setattr(
        race_postprocess, "_build_wind_snapshot", fake_build_wind_snapshot
    )
    monkeypatch.setattr(
        race_postprocess, "generate_summary", fake_generate_summary
    )
    return s


def _patch_loads(
    monkeypatch: pytest.MonkeyPatch,
    *,
    race: Optional[dict],
    track: list[dict],
) -> None:
    async def fake_load_race(pool, race_id):
        return race

    async def fake_load_track(pool, race_id):
        return track

    monkeypatch.setattr(race_postprocess, "_load_race", fake_load_race)
    monkeypatch.setattr(race_postprocess, "_load_track", fake_load_track)


# ─── Decision-branch tests ────────────────────────────────────────────


async def test_returns_0_when_race_missing(monkeypatch, spy):
    _patch_loads(monkeypatch, race=None, track=[])
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert spy.persist_calls == []
    assert spy.summary_calls == 0


async def test_returns_0_when_no_track_points(monkeypatch, spy):
    _patch_loads(monkeypatch, race=_make_race_row(), track=[])
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert spy.persist_calls == []
    assert spy.summary_calls == 0


async def test_generates_summary_when_missing(monkeypatch, spy):
    _patch_loads(monkeypatch, race=_make_race_row(), track=_make_track_rows())
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert spy.summary_calls == 1
    assert spy.wind_calls == 1
    assert len(spy.persist_calls) == 1
    call = spy.persist_calls[0]
    assert call["ai_summary"] is not None
    assert call["wind_snapshot"] is not None


async def test_skips_when_summary_current_and_snapshot_present(monkeypatch, spy):
    race = _make_race_row(
        ai_summary={
            "recap": "previously generated",
            "tips": [],
            "model": "test",
            "prompt_version": PROMPT_VERSION,
        },
        wind_snapshot={"already": "there"},
    )
    _patch_loads(monkeypatch, race=race, track=_make_track_rows())
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    # Neither AI nor wind ran.
    assert spy.summary_calls == 0
    assert spy.wind_calls == 0
    # _persist still called but with both fields None — no-op UPDATE.
    assert len(spy.persist_calls) == 1
    assert spy.persist_calls[0] == {"ai_summary": None, "wind_snapshot": None}


async def test_force_regenerates_both(monkeypatch, spy):
    race = _make_race_row(
        ai_summary={
            "recap": "x", "tips": [],
            "model": "test", "prompt_version": PROMPT_VERSION,
        },
        wind_snapshot={"already": "there"},
    )
    _patch_loads(monkeypatch, race=race, track=_make_track_rows())
    rc = await race_postprocess.process_race(FakePool(), RACE_ID, force=True)
    assert rc == 0
    assert spy.summary_calls == 1
    assert spy.wind_calls == 1


async def test_stale_prompt_version_triggers_summary_regen(monkeypatch, spy):
    race = _make_race_row(
        ai_summary={
            "recap": "older", "tips": [],
            "model": "test", "prompt_version": PROMPT_VERSION - 1,
        },
        wind_snapshot={"already": "there"},
    )
    _patch_loads(monkeypatch, race=race, track=_make_track_rows())
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert spy.summary_calls == 1
    # Wind snapshot kept (already present, not forced).
    assert spy.wind_calls == 0


async def test_snapshot_missing_triggers_wind_build_even_if_summary_current(
    monkeypatch, spy
):
    race = _make_race_row(
        ai_summary={
            "recap": "ok", "tips": [],
            "model": "test", "prompt_version": PROMPT_VERSION,
        },
        wind_snapshot=None,
    )
    _patch_loads(monkeypatch, race=race, track=_make_track_rows())
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert spy.wind_calls == 1
    assert spy.summary_calls == 0


async def test_generate_summary_failure_leaves_existing_intact(
    monkeypatch, spy
):
    # Override the spy's generate_summary to return None.
    def fake_gen(**kwargs):
        spy.summary_calls += 1
        return None
    monkeypatch.setattr(race_postprocess, "generate_summary", fake_gen)

    _patch_loads(
        monkeypatch,
        race=_make_race_row(),     # missing summary, will try to regen
        track=_make_track_rows(),
    )
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert spy.summary_calls == 1
    # _persist still called with ai_summary=None (skipped); wind still ok.
    assert len(spy.persist_calls) == 1
    assert spy.persist_calls[0]["ai_summary"] is None
    assert spy.persist_calls[0]["wind_snapshot"] is not None
