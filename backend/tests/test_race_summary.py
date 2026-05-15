"""Tests for app/services/race_summary.py.

The Anthropic call is mocked end-to-end — tests don't hit the real
API. We exercise:
  * the deterministic prompt builder
  * the response parser, including the forgiving JSON extraction
  * the generate_summary wrapper with an injected fake client
  * the no-key-graceful-degrade path
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.services import race_summary
from app.services.race_summary import (
    PROMPT_VERSION,
    build_prompt,
    generate_summary,
    parse_response,
)


# ─── Fixture stats / wind ─────────────────────────────────────────────


def _stats(
    *,
    distance_m: float = 4000.0,
    elapsed_s: float = 1800.0,
    avg_sog_kt: float = 4.3,
    avg_moving_sog_kt: float = 4.5,
    max_sog_kt: float = 6.8,
    stopped_s: float = 60.0,
    legs: list[dict] | None = None,
) -> dict:
    return {
        "point_count": 1800,
        "started_at": "2026-05-14T18:00:00+00:00",
        "ended_at": "2026-05-14T18:30:00+00:00",
        "elapsed_s": elapsed_s,
        "moving_s": elapsed_s - stopped_s,
        "stopped_s": stopped_s,
        "distance_m": distance_m,
        "avg_sog_kt": avg_sog_kt,
        "avg_moving_sog_kt": avg_moving_sog_kt,
        "max_sog_kt": max_sog_kt,
        "legs": legs or [
            {
                "leg_index": 0,
                "from_label": "Start",
                "to_label": "A",
                "start_ts": "2026-05-14T18:00:00+00:00",
                "end_ts": "2026-05-14T18:10:00+00:00",
                "elapsed_s": 600.0,
                "distance_m": 1500.0,
                "avg_sog_kt": 4.9,
            },
            {
                "leg_index": 1,
                "from_label": "A",
                "to_label": "Finish",
                "start_ts": "2026-05-14T18:10:00+00:00",
                "end_ts": "2026-05-14T18:30:00+00:00",
                "elapsed_s": 1200.0,
                "distance_m": 2500.0,
                "avg_sog_kt": 4.1,
            },
        ],
        "speed_series": [],
    }


def _wind_snapshot() -> dict:
    # Snapshot that produces a non-null summary: 1 time, 2x2 grid, all
    # filled, ~10 kt westerly.
    return {
        "bbox": [42.0, -87.7, 42.1, -87.5],
        "grid_deg": 0.1,
        "lats": [42.0, 42.1],
        "lons": [-87.7, -87.5],
        "t_start": "2026-05-14T18:00:00+00:00",
        "t_end": "2026-05-14T18:30:00+00:00",
        "dt_s": 900,
        "times": ["2026-05-14T18:00:00+00:00"],
        "source": "hybrid",
        "u_mps": [[[5.144, 5.144], [5.144, 5.144]]],
        "v_mps": [[[0.0, 0.0], [0.0, 0.0]]],
    }


# ─── build_prompt ─────────────────────────────────────────────────────


def test_prompt_includes_basic_race_facts():
    p = build_prompt(
        race_name="Tuesday Beer Can #3",
        boat_class="Beneteau 36.7",
        stats=_stats(),
    )
    assert "Tuesday Beer Can #3" in p
    assert "Beneteau 36.7" in p
    # Elapsed shown as M:SS or H format.
    assert "30:00" in p or "1800" in p


def test_prompt_includes_leg_lines():
    p = build_prompt(race_name=None, boat_class=None, stats=_stats())
    assert "Leg 1: Start → A" in p
    assert "Leg 2: A → Finish" in p


def test_prompt_handles_no_legs():
    s = _stats()
    s["legs"] = []
    p = build_prompt(race_name=None, boat_class=None, stats=s)
    assert "no marks rounded" in p.lower() or "dnf" in p.lower()


def test_prompt_includes_wind_summary_when_snapshot_present():
    p = build_prompt(
        race_name=None,
        boat_class=None,
        stats=_stats(),
        wind_snapshot=_wind_snapshot(),
    )
    # 10 kt westerly: speed ~10.0 and cardinal "W".
    assert "10.0 kt" in p
    assert "W)" in p or "W " in p  # WSW/WNW/W all contain W


def test_prompt_includes_corrected_time_when_present():
    s = _stats()
    s["corrected_time_s"] = 1500.0
    s["corrected_using"] = "hcp"
    s["rating_seconds_per_mile"] = 75
    p = build_prompt(race_name=None, boat_class=None, stats=s)
    assert "Corrected time" in p
    assert "rating 75" in p
    assert "ToD HCP" in p


def test_prompt_omits_corrected_time_when_no_rating():
    s = _stats()
    # corrected_time_s / corrected_using / rating all unset.
    p = build_prompt(race_name=None, boat_class=None, stats=s)
    assert "Corrected time" not in p


def test_prompt_notes_missing_wind():
    p = build_prompt(
        race_name=None,
        boat_class=None,
        stats=_stats(),
        wind_snapshot=None,
    )
    assert "wind data: not available" in p.lower()


def test_prompt_handles_partial_wind_coverage():
    # All-null snapshot → "no forecast coverage" branch.
    null_snap = _wind_snapshot()
    null_snap["u_mps"] = [[[None, None], [None, None]]]
    null_snap["v_mps"] = [[[None, None], [None, None]]]
    p = build_prompt(
        race_name=None,
        boat_class=None,
        stats=_stats(),
        wind_snapshot=null_snap,
    )
    assert "no forecast coverage" in p.lower()


# ─── parse_response ──────────────────────────────────────────────────


def test_parse_strict_json():
    raw = '{"recap": "Solid race.", "tips": ["Keep going."]}'
    out = parse_response(raw)
    assert out == {"recap": "Solid race.", "tips": ["Keep going."]}


def test_parse_extracts_json_from_prose():
    raw = (
        "Here's your debrief:\n"
        '{"recap": "Tight tacking duel.", "tips": ["Trim earlier."]}'
        "\nLet me know if you want more."
    )
    out = parse_response(raw)
    assert out is not None
    assert "Tight tacking duel" in out["recap"]


def test_parse_extracts_from_code_fence():
    raw = (
        "```json\n"
        '{"recap": "Clean.", "tips": ["x", "y"]}\n'
        "```"
    )
    out = parse_response(raw)
    assert out is not None
    assert out["tips"] == ["x", "y"]


def test_parse_drops_non_string_tips():
    raw = '{"recap": "ok", "tips": ["good", 5, null, "also good"]}'
    out = parse_response(raw)
    assert out is not None
    assert out["tips"] == ["good", "also good"]


def test_parse_returns_none_on_malformed():
    assert parse_response("nothing here") is None
    assert parse_response("") is None
    assert parse_response('{"recap": 5}') is None  # recap not a string


def test_parse_returns_none_on_empty_recap_field():
    # recap must exist as a string; empty string is allowed but
    # missing key should fail.
    assert parse_response('{"tips": ["a"]}') is None


# ─── generate_summary with fake client ───────────────────────────────


@dataclass
class _FakeBlock:
    text: str


@dataclass
class _FakeMessage:
    content: list[_FakeBlock]


class _FakeMessages:
    def __init__(self, text: str, *, raise_exc: Exception | None = None):
        self._text = text
        self._raise_exc = raise_exc
        self.last_call: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.last_call = kwargs
        if self._raise_exc:
            raise self._raise_exc
        return _FakeMessage(content=[_FakeBlock(text=self._text)])


class _FakeClient:
    def __init__(self, text: str, *, raise_exc: Exception | None = None):
        self.messages = _FakeMessages(text, raise_exc=raise_exc)


def test_generate_summary_happy_path():
    client = _FakeClient('{"recap": "Good race.", "tips": ["a", "b"]}')
    out = generate_summary(
        race_name="Test",
        boat_class="Etchells",
        stats=_stats(),
        wind_snapshot=_wind_snapshot(),
        client=client,
        model="test-model",
    )
    assert out is not None
    assert out["recap"] == "Good race."
    assert out["tips"] == ["a", "b"]
    assert out["model"] == "test-model"
    assert out["prompt_version"] == PROMPT_VERSION
    assert "generated_at" in out
    # System prompt and user prompt were passed.
    call = client.messages.last_call
    assert call["model"] == "test-model"
    assert "sailing race coach" in call["system"].lower()


def test_generate_summary_returns_none_on_api_error():
    client = _FakeClient(
        "ignored", raise_exc=RuntimeError("anthropic 429 rate limit"),
    )
    out = generate_summary(
        race_name="Test",
        boat_class=None,
        stats=_stats(),
        client=client,
    )
    assert out is None


def test_generate_summary_returns_none_when_response_unparseable():
    client = _FakeClient("the model said no")
    out = generate_summary(
        race_name=None,
        boat_class=None,
        stats=_stats(),
        client=client,
    )
    assert out is None


def test_generate_summary_returns_none_when_no_api_key(monkeypatch: pytest.MonkeyPatch):
    """No client passed AND no key in settings — should not raise."""
    # Stub the lazy import. race_summary calls `from app.config import
    # get_settings` inside generate_summary; we replace get_settings
    # in the app.config namespace with a fake returning a key-less
    # settings object.
    fake_settings = type(
        "FakeSettings", (), {"anthropic_api_key": None, "anthropic_model": "x"}
    )()

    import app.config
    monkeypatch.setattr(
        app.config, "get_settings", lambda: fake_settings, raising=False
    )
    out = generate_summary(
        race_name=None, boat_class=None, stats=_stats(),
    )
    assert out is None
