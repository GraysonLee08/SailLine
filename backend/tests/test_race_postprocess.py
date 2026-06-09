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
    heel_summary: Optional[dict] = None,
    performance_summary: Optional[dict] = None,
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
        "heel_summary": heel_summary,
        "performance_summary": performance_summary,
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
        self.summary_kwargs: list[dict] = []


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch):
    s = _Spy()

    async def fake_persist(
        pool, race_id, *, ai_summary, wind_snapshot, heel_summary,
        performance_summary=None,
    ):
        s.persist_calls.append(
            {
                "ai_summary": ai_summary,
                "wind_snapshot": wind_snapshot,
                "heel_summary": heel_summary,
                "performance_summary": performance_summary,
            }
        )

    async def fake_build_wind_snapshot(**kwargs: Any):
        s.wind_calls += 1
        return {"fake": "snapshot"}

    def fake_generate_summary(**kwargs: Any):
        s.summary_calls += 1
        s.summary_kwargs.append(kwargs)
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
    imu: Optional[list[dict]] = None,
    calibrations: Optional[list[dict]] = None,
) -> None:
    async def fake_load_race(pool, race_id):
        return race

    async def fake_load_track(pool, race_id):
        return track

    async def fake_load_imu_samples(pool, race_id):
        return imu or []

    async def fake_load_calibrations(pool, race_id):
        return calibrations or []

    monkeypatch.setattr(race_postprocess, "_load_race", fake_load_race)
    monkeypatch.setattr(race_postprocess, "_load_track", fake_load_track)
    monkeypatch.setattr(
        race_postprocess, "_load_imu_samples", fake_load_imu_samples
    )
    monkeypatch.setattr(
        race_postprocess, "_load_calibrations", fake_load_calibrations
    )


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
    # _persist still called but with all fields None — no-op UPDATE.
    # heel_summary is also None here because the heel_summary column
    # was non-null in the seed row, so the "backfill" branch was skipped.
    assert len(spy.persist_calls) == 1
    assert spy.persist_calls[0] == {
        "ai_summary": None, "wind_snapshot": None, "heel_summary": None,
        "performance_summary": None,
    }


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


# ─── Heel summary plumbing ────────────────────────────────────────────


def _make_imu_rows(n: int = 30) -> list[dict]:
    """Synthetic IMU samples — heel oscillating around 12°, pitch ~0°."""
    rows = []
    for i in range(n):
        rows.append({
            "recorded_at": T0 + timedelta(seconds=i),
            "heel_deg": 12.0 + (i % 5) * 2.0,
            "pitch_deg": 2.0,
            "yaw_deg": 90.0,
        })
    return rows


async def test_heel_summary_passed_to_generate_summary_when_imu_present(
    monkeypatch, spy,
):
    _patch_loads(
        monkeypatch,
        race=_make_race_row(),
        track=_make_track_rows(),
        imu=_make_imu_rows(),
        calibrations=[],
    )
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert spy.summary_calls == 1
    kwargs = spy.summary_kwargs[0]
    assert "heel_summary" in kwargs
    heel = kwargs["heel_summary"]
    assert heel is not None
    assert heel["sample_count"] == 30
    assert heel["max_heel_abs_deg"] >= 12.0


async def test_heel_summary_none_when_no_imu_rows(monkeypatch, spy):
    _patch_loads(
        monkeypatch,
        race=_make_race_row(),
        track=_make_track_rows(),
        imu=[],
        calibrations=[],
    )
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    kwargs = spy.summary_kwargs[0]
    assert kwargs.get("heel_summary") is None


async def test_imu_load_failure_does_not_break_postprocess(monkeypatch, spy):
    """A DB exception loading IMU should be swallowed; the AI summary
    still runs (without heel data)."""
    async def boom(pool, race_id):
        raise RuntimeError("simulated DB error")

    _patch_loads(
        monkeypatch,
        race=_make_race_row(),
        track=_make_track_rows(),
        imu=[],
        calibrations=[],
    )
    monkeypatch.setattr(race_postprocess, "_load_imu_samples", boom)

    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert spy.summary_calls == 1
    # heel_summary kwarg should be None (graceful degrade).
    assert spy.summary_kwargs[0].get("heel_summary") is None


async def test_calibration_offsets_applied_in_postprocess(monkeypatch, spy):
    """A non-zero calibration row should shift the computed max_heel."""
    cal = [{
        "captured_at": T0 - timedelta(seconds=1),
        "heel_zero_offset_deg": 10.0,
        "pitch_zero_offset_deg": 0.0,
    }]
    _patch_loads(
        monkeypatch,
        race=_make_race_row(),
        track=_make_track_rows(),
        imu=_make_imu_rows(),
        calibrations=cal,
    )
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    heel = spy.summary_kwargs[0]["heel_summary"]
    # Raw max was ~20° (12 + 4×2). After subtracting a 10° offset, the
    # max should drop close to ~10°.
    assert heel is not None
    assert heel["max_heel_abs_deg"] < 12.0


# ─── heel_summary column persistence (migration 0016) ─────────────────


async def test_heel_summary_persisted_when_computed(monkeypatch, spy):
    """When IMU samples exist and the AI summary regenerates, the
    computed heel_summary dict is passed to _persist so it lands on
    the race_sessions.heel_summary column."""
    _patch_loads(
        monkeypatch,
        race=_make_race_row(),
        track=_make_track_rows(),
        imu=_make_imu_rows(),
        calibrations=[],
    )
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert len(spy.persist_calls) == 1
    call = spy.persist_calls[0]
    assert call["heel_summary"] is not None
    assert call["heel_summary"]["sample_count"] == 30


async def test_heel_summary_backfill_when_column_null_but_ai_current(
    monkeypatch, spy,
):
    """When ai_summary is current but heel_summary column is null
    (race processed before migration 0016 shipped), the next
    postprocess run should recompute heel just to backfill the
    column — even though the AI step is skipped."""
    race = _make_race_row(
        ai_summary={
            "recap": "previously generated", "tips": [],
            "model": "test", "prompt_version": PROMPT_VERSION,
        },
        wind_snapshot={"already": "there"},
        heel_summary=None,
    )
    _patch_loads(
        monkeypatch,
        race=race,
        track=_make_track_rows(),
        imu=_make_imu_rows(),
        calibrations=[],
    )
    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    # AI was skipped (current); wind was skipped (present).
    assert spy.summary_calls == 0
    assert spy.wind_calls == 0
    # But heel_summary was computed and persisted.
    assert len(spy.persist_calls) == 1
    assert spy.persist_calls[0]["heel_summary"] is not None
    assert spy.persist_calls[0]["ai_summary"] is None
    assert spy.persist_calls[0]["wind_snapshot"] is None


async def test_heel_summary_not_recomputed_when_column_present_and_ai_current(
    monkeypatch, spy,
):
    """Steady-state idempotency: when both AI summary and heel_summary
    are already on the row and prompt version matches, the postprocess
    job should do nothing — no IMU load, no compute, no overwrite."""
    race = _make_race_row(
        ai_summary={
            "recap": "previously generated", "tips": [],
            "model": "test", "prompt_version": PROMPT_VERSION,
        },
        wind_snapshot={"already": "there"},
        heel_summary={
            "sample_count": 42,
            "max_heel_deg": 25.0,
            "max_heel_abs_deg": 25.0,
            "avg_heel_abs_deg": 15.0,
            "pct_time_heeled_gt_10": 0.7,
            "pct_time_heeled_gt_20": 0.4,
            "max_pitch_abs_deg": 5.0,
            "by_leg": [],
        },
        # Present so the performance backfill branch is also skipped —
        # this test asserts a true steady-state no-op.
        performance_summary={
            "sample_count": 42,
            "avg_speed_ratio": 0.95,
            "avg_vmg_efficiency": 0.9,
            "pct_time_on_target": 0.5,
            "avg_target_kts": 6.0,
            "avg_actual_kts": 5.7,
            "by_leg": [],
        },
    )

    # Track IMU loads to confirm we never reached them.
    imu_load_calls = {"n": 0}

    async def counting_imu_load(pool, race_id):
        imu_load_calls["n"] += 1
        return _make_imu_rows()

    _patch_loads(
        monkeypatch,
        race=race,
        track=_make_track_rows(),
        imu=[], calibrations=[],
    )
    monkeypatch.setattr(
        race_postprocess, "_load_imu_samples", counting_imu_load
    )

    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert imu_load_calls["n"] == 0
    assert len(spy.persist_calls) == 1
    assert spy.persist_calls[0] == {
        "ai_summary": None, "wind_snapshot": None, "heel_summary": None,
        "performance_summary": None,
    }


async def test_force_recomputes_heel_summary(monkeypatch, spy):
    """--force should recompute heel_summary even if ai_summary and
    heel_summary are both already current."""
    race = _make_race_row(
        ai_summary={
            "recap": "x", "tips": [],
            "model": "test", "prompt_version": PROMPT_VERSION,
        },
        wind_snapshot={"already": "there"},
        heel_summary={"sample_count": 1, "max_heel_deg": 1.0,
                      "max_heel_abs_deg": 1.0, "avg_heel_abs_deg": 1.0,
                      "pct_time_heeled_gt_10": 0.0,
                      "pct_time_heeled_gt_20": 0.0,
                      "max_pitch_abs_deg": 0.0, "by_leg": []},
    )
    _patch_loads(
        monkeypatch,
        race=race,
        track=_make_track_rows(),
        imu=_make_imu_rows(),
        calibrations=[],
    )
    rc = await race_postprocess.process_race(FakePool(), RACE_ID, force=True)
    assert rc == 0
    # All three branches ran.
    assert spy.summary_calls == 1
    assert spy.wind_calls == 1
    assert len(spy.persist_calls) == 1
    call = spy.persist_calls[0]
    assert call["heel_summary"] is not None
    assert call["heel_summary"]["sample_count"] == 30


# ─── performance_summary plumbing (migration 0017) ────────────────────


async def test_performance_summary_persisted_when_scoreable(monkeypatch, spy):
    """When a wind snapshot is available, the worker scores the track
    and persists the result to the performance_summary column. We patch
    the engine to a sentinel and assert the wiring (polar + wind sampler
    + mark passes) reaches it."""
    captured: dict = {}

    def fake_perf(track_points, *, wind_sampler, polar, mark_passes=None):
        captured["wind_sampler"] = wind_sampler
        captured["polar"] = polar
        captured["mark_passes"] = mark_passes
        return {"sample_count": 7, "avg_speed_ratio": 0.97,
                "avg_vmg_efficiency": 0.9, "pct_time_on_target": 0.6,
                "avg_target_kts": 6.0, "avg_actual_kts": 5.8, "by_leg": []}

    monkeypatch.setattr(race_postprocess, "compute_performance_summary", fake_perf)
    _patch_loads(monkeypatch, race=_make_race_row(), track=_make_track_rows())

    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert len(spy.persist_calls) == 1
    perf = spy.persist_calls[0]["performance_summary"]
    assert perf is not None
    assert perf["sample_count"] == 7
    # Wiring reached the engine.
    assert callable(captured["wind_sampler"])
    assert captured["polar"] is not None
    assert captured["mark_passes"] is not None


async def test_performance_summary_skipped_when_no_wind_snapshot(monkeypatch, spy):
    """No snapshot (none on the row, none built) → the engine isn't even
    called and performance_summary persists as None."""
    calls = {"n": 0}

    def counting_perf(*args, **kwargs):
        calls["n"] += 1
        return {"sample_count": 1}

    async def no_snapshot(**kwargs):
        spy.wind_calls += 1
        return None

    monkeypatch.setattr(race_postprocess, "compute_performance_summary", counting_perf)
    monkeypatch.setattr(race_postprocess, "_build_wind_snapshot", no_snapshot)
    _patch_loads(
        monkeypatch,
        race=_make_race_row(wind_snapshot=None),
        track=_make_track_rows(),
    )

    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    assert calls["n"] == 0
    assert spy.persist_calls[0]["performance_summary"] is None


async def test_performance_backfill_when_column_null_but_ai_current(monkeypatch, spy):
    """ai_summary current + wind snapshot present + performance_summary
    column null → backfill: the engine runs and persists even though AI
    and wind steps are skipped. Mirrors the heel backfill branch."""
    def fake_perf(track_points, *, wind_sampler, polar, mark_passes=None):
        return {"sample_count": 3, "avg_speed_ratio": 1.0,
                "avg_vmg_efficiency": 1.0, "pct_time_on_target": 1.0,
                "avg_target_kts": 6.0, "avg_actual_kts": 6.0, "by_leg": []}

    monkeypatch.setattr(race_postprocess, "compute_performance_summary", fake_perf)
    race = _make_race_row(
        ai_summary={
            "recap": "current", "tips": [],
            "model": "test", "prompt_version": PROMPT_VERSION,
        },
        wind_snapshot={"already": "there"},
        performance_summary=None,
    )
    _patch_loads(monkeypatch, race=race, track=_make_track_rows())

    rc = await race_postprocess.process_race(FakePool(), RACE_ID)
    assert rc == 0
    # AI + wind skipped, performance backfilled.
    assert spy.summary_calls == 0
    assert spy.wind_calls == 0
    assert len(spy.persist_calls) == 1
    assert spy.persist_calls[0]["ai_summary"] is None
    assert spy.persist_calls[0]["wind_snapshot"] is None
    assert spy.persist_calls[0]["performance_summary"] is not None
    assert spy.persist_calls[0]["performance_summary"]["sample_count"] == 3


# ─── Cold-import regression (circular import guard) ────────────────────


def test_worker_imports_in_a_fresh_interpreter():
    """The worker must import cleanly from a COLD interpreter.

    workers/race_postprocess.py imports app.services.weather first, which
    on 2026-06-09 closed a circular import (weather → routing → weather)
    and crashed every job at load with ImportError. The in-process test
    suite masks this: by the time this file's top-level
    ``from workers import race_postprocess`` runs, conftest/other tests
    have already imported those packages in a safe order, so the module
    is cached and the cycle never re-triggers. A subprocess reproduces
    the worker's real cold-start import order, so a re-introduced cycle
    fails here instead of only in production.
    """
    import subprocess
    import sys
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", "import workers.race_postprocess"],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "worker failed to cold-import (circular import?):\n" + result.stderr
    )
