"""Tests for the tactician replay harness (Phase B, 2026-07-09).

Replays the committed Beer Can Race 4 fixture (4,543 real GPS points,
Chicago, 2026-06-03) with synthetic steady wind and the deterministic
stub advisor. Asserts the harness runs end-to-end, honours the same
cooldown semantics as the live pipeline, and is deterministic — so a
threshold change that alters replay behaviour shows up as a test diff,
not an on-water surprise.

Real-Claude smoke lives behind ``SAILLINE_AI_SMOKE=1`` (repo pattern).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.tactics.pipeline import GLOBAL_COOLDOWN_S
from app.services.tactics.trace import EXIT_REASONS
from tools.tactics_replay import (
    SteadyWind,
    load_fixture,
    replay,
    stub_advisor,
)

FIXTURE = Path(__file__).parent / "fixtures" / "beer_can_race_4_20260603.json"


@pytest.fixture(scope="module")
def beer_can_data():
    return load_fixture(FIXTURE)


@pytest.fixture(scope="module")
def report(beer_can_data):
    # Southerly 12 kt — arbitrary but fixed; the assertions below only
    # rely on structure + determinism, not on which detectors fire.
    return replay(
        beer_can_data,
        wind=SteadyWind(180.0, 12.0),
        batch_seconds=30,
    )


# ─── Loading ─────────────────────────────────────────────────────────


def test_fixture_loads_and_derives_cog(beer_can_data):
    track = beer_can_data["track"]
    assert len(track) == 4543
    assert all(p["cog_deg"] is not None for p in track)
    assert all(0.0 <= p["cog_deg"] < 360.0 for p in track)
    # Timestamps parsed to aware datetimes, ascending.
    assert track[0]["t"].tzinfo is not None
    assert track[0]["t"] < track[-1]["t"]
    assert beer_can_data["boat_class"] == "Beneteau First 36.7"
    assert len(beer_can_data["marks"]) == 6


# ─── Replay structure ────────────────────────────────────────────────


def test_replay_covers_whole_track(report, beer_can_data):
    track = beer_can_data["track"]
    span_s = (track[-1]["t"] - track[0]["t"]).total_seconds()
    expected = int(span_s // 30)
    assert abs(report["summary"]["evaluations"] - expected) <= 1
    assert report["meta"]["wind"] == "synthetic"


def test_replay_exit_reasons_are_pinned(report):
    """Every replay exit must be a trace-vocabulary exit — the harness
    and the live pipeline stay in the same language."""
    seen = {e["exit"] for e in report["evaluations"]}
    assert seen <= EXIT_REASONS
    assert None not in seen


def test_replay_respects_global_cooldown(report):
    """No two non-skipped evaluations closer than the global window."""
    active = [
        datetime.fromisoformat(e["t"])
        for e in report["evaluations"]
        if e["exit"] not in ("cooldown_global",)
        and e["exit"] not in ("insufficient_track", "no_forecast")
    ]
    gap = timedelta(seconds=GLOBAL_COOLDOWN_S)
    for a, b in zip(active, active[1:]):
        assert b - a >= gap


def test_replay_calls_come_from_stub(report):
    for call in report["summary"]["calls"]:
        assert call["model"] == "stub"
        assert call["message"].startswith("[stub ")


def test_replay_summary_consistent(report):
    s = report["summary"]
    assert s["exits"].get("published", 0) == len(s["calls"])
    total = sum(s["exits"].values())
    assert total == s["evaluations"] == len(report["evaluations"])
    for d in s["detectors"].values():
        assert d["won"] <= d["fired"]


def test_replay_is_deterministic(beer_can_data, report):
    again = replay(
        beer_can_data,
        wind=SteadyWind(180.0, 12.0),
        batch_seconds=30,
    )
    assert json.dumps(again, sort_keys=True, default=str) == json.dumps(
        report, sort_keys=True, default=str,
    )


def test_replay_json_serializable(report):
    json.dumps(report, default=str)  # must not raise


# ─── Advisor plumbing ────────────────────────────────────────────────


def test_stub_advisor_formats_diagnosis():
    from app.services.tactics.detectors import CallCandidate

    cand = CallCandidate(
        call_type="off_pace", call_class="coaching",
        diagnosis={"speed_ratio": 0.82, "window_s": 90},
    )
    out = stub_advisor({}, cand)
    assert out["model"] == "stub"
    assert "off_pace" in out["message"]
    assert "speed_ratio=0.82" in out["message"]


def test_replay_advisor_none_becomes_silent_exit(beer_can_data):
    """A SILENT/failed advisor must surface as its own exit, and the
    per-type cooldown stays burned (pipeline parity: SETNX already
    succeeded before the model call)."""
    result = replay(
        beer_can_data,
        wind=SteadyWind(180.0, 12.0),
        batch_seconds=30,
        advisor_fn=lambda snapshot, winner: None,
    )
    s = result["summary"]
    assert s["exits"].get("published", 0) == 0
    assert s["calls"] == []
    # If any detector fired at all, the silent exits must be recorded.
    if any(d["won"] for d in s["detectors"].values()):
        assert s["exits"].get("advisor_silent_or_failed", 0) > 0


# ─── Real-API smoke (opt-in) ─────────────────────────────────────────


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("SAILLINE_AI_SMOKE") != "1",
    reason="set SAILLINE_AI_SMOKE=1 for the real-Anthropic smoke test",
)
def test_replay_real_claude_smoke(beer_can_data):
    from tools.tactics_replay import real_advisor

    result = replay(
        beer_can_data,
        wind=SteadyWind(180.0, 12.0),
        batch_seconds=30,
        advisor_fn=real_advisor,
    )
    # The model may answer SILENT on any given call; the smoke only
    # asserts the round trip doesn't error and messages honour ≤140.
    for call in result["summary"]["calls"]:
        assert len(call["message"]) <= 140
