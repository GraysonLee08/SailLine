"""Tests for app/services/race_summary.py (prompt v4).

The Anthropic call is mocked end-to-end — tests don't hit the real
API. We exercise:
  * the deterministic prompt builder (payload → user message)
  * the response parser, including the forgiving JSON extraction and
    shape coercion (tags, cost_s, directives)
  * the generate_summary wrapper with an injected fake client,
    including signature attachment to the playbook
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


# ─── Fixture payload ──────────────────────────────────────────────────


def _payload(**overrides: Any) -> dict:
    base = {
        "boat": {"class": "Beneteau 36.7", "loa_ft": 36.7},
        "course": {"mode": "inshore", "marks_count": 3, "legs_count": 2},
        "start": {
            "line_bearing_deg": 310.0,
            "bias_deg": 6.0,
            "favored_end": "A",
            "end_started": "B",
            "distance_to_line_at_gun_m": 41.0,
            "sog_at_gun_kts": 5.1,
            "time_to_cross_s": 14.0,
            "ocs": False,
        },
        "legs": [
            {"n": 1, "type": "upwind", "elapsed_s": 900.0,
             "sailed_ratio": 1.41, "speed_ratio": 0.93,
             "pct_time_lifted": 0.4, "pct_time_headed": 0.35,
             "tacks": 6},
            {"n": 2, "type": "run", "elapsed_s": 700.0,
             "sailed_ratio": 1.05, "speed_ratio": 0.97, "gybes": 2},
        ],
        "maneuvers": {
            "tacks": {"count": 6, "mean_loss_bl": 2.4, "worst_loss_bl": 5.0},
            "gybes": {"count": 2, "mean_loss_bl": 1.1, "worst_loss_bl": 1.5},
        },
        "condition_signature": {
            "tws_lo_kts": 8.0, "tws_hi_kts": 12.0,
            "twd_mean_deg": 220.0, "character": "oscillating",
            "osc_amplitude_deg": 12.0, "tws_trend": "steady",
        },
        "condition_signature_text": "TWS 8-12 kt, TWD ~220° with oscillating ±12°",
        "result": {"elapsed_s": 1600.0, "corrected_s": 1450.0},
    }
    base.update(overrides)
    return base


_GOOD_RESPONSE = (
    '{"summary": "Oscillating southwesterly; the beat decided it.",'
    ' "what_worked": ["Start 2 BL from the favored end with speed"],'
    ' "what_cost": ['
    '   {"tag": "EXECUTION", "text": "6 tacks at 2.4 BL mean", "cost_s": 45},'
    '   {"tag": "DECISION", "text": "35% of leg 1 sailed headed", "cost_s": 60}'
    ' ],'
    ' "total_identifiable_loss_s": 105,'
    ' "playbook_directives": ["Tack on headers >= 8 deg", "Two-tack beats only"]}'
)


# ─── build_prompt ─────────────────────────────────────────────────────


def test_prompt_wraps_payload_in_race_data_tags():
    p = build_prompt(_payload())
    assert p.startswith("Analyze this race.")
    assert "<race_data>" in p and "</race_data>" in p


def test_prompt_serialises_payload_content():
    p = build_prompt(_payload())
    assert "Beneteau 36.7" in p
    assert "oscillating" in p
    assert "favored_end" in p


# ─── parse_response ──────────────────────────────────────────────────


def test_parse_strict_json():
    out = parse_response(_GOOD_RESPONSE)
    assert out is not None
    assert out["summary"].startswith("Oscillating")
    assert len(out["what_worked"]) == 1
    assert len(out["what_cost"]) == 2
    assert out["what_cost"][0]["tag"] == "EXECUTION"
    assert out["what_cost"][0]["cost_s"] == 45.0
    assert out["total_identifiable_loss_s"] == 105.0
    assert out["playbook_directives"] == [
        "Tack on headers >= 8 deg", "Two-tack beats only",
    ]


def test_parse_extracts_json_from_prose_and_fences():
    raw = "Here's the analysis:\n```json\n" + _GOOD_RESPONSE + "\n```\nDone."
    out = parse_response(raw)
    assert out is not None
    assert out["summary"].startswith("Oscillating")


def test_parse_coerces_bad_tag_to_decision():
    raw = (
        '{"summary": "s", "what_cost": '
        '[{"tag": "WHATEVER", "text": "x", "cost_s": "not a number"}]}'
    )
    out = parse_response(raw)
    assert out is not None
    assert out["what_cost"][0]["tag"] == "DECISION"
    assert out["what_cost"][0]["cost_s"] is None


def test_parse_drops_non_string_directives_and_findings():
    raw = (
        '{"summary": "s",'
        ' "what_worked": ["ok", 5, null],'
        ' "what_cost": [{"tag": "EXECUTION", "text": "x"}, {"no": "text"}, "str"],'
        ' "playbook_directives": ["a", 3]}'
    )
    out = parse_response(raw)
    assert out is not None
    assert out["what_worked"] == ["ok"]
    assert len(out["what_cost"]) == 1
    assert out["playbook_directives"] == ["a"]


def test_parse_returns_none_on_malformed():
    assert parse_response("nothing here") is None
    assert parse_response("") is None
    assert parse_response('{"summary": 5}') is None      # summary not a string
    assert parse_response('{"summary": "  "}') is None   # empty summary
    assert parse_response('{"what_worked": ["a"]}') is None  # missing summary


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
    client = _FakeClient(_GOOD_RESPONSE)
    out = generate_summary(
        race_name="Beer Can 7.2", payload=_payload(),
        client=client, model="test-model",
    )
    assert out is not None
    assert out["summary"].startswith("Oscillating")
    assert out["model"] == "test-model"
    assert out["prompt_version"] == PROMPT_VERSION
    assert "generated_at" in out
    # The derived payload is persisted alongside the model output.
    assert out["analysis"]["boat"]["class"] == "Beneteau 36.7"
    # System + user prompt were passed; race name injected.
    call = client.messages.last_call
    assert call["model"] == "test-model"
    assert "sailing race coach" in call["system"].lower()
    assert "Beer Can 7.2" in call["messages"][0]["content"]


def test_generate_summary_attaches_computed_signature_to_playbook():
    """The signature comes from signature.py via the payload — the
    model only writes directives."""
    client = _FakeClient(_GOOD_RESPONSE)
    out = generate_summary(
        race_name=None, payload=_payload(), client=client, model="m",
    )
    assert out is not None
    pb = out["playbook"]
    assert pb["directives"] == ["Tack on headers >= 8 deg", "Two-tack beats only"]
    assert pb["signature"]["character"] == "oscillating"
    assert "±12" in pb["signature_text"] or "oscillating" in pb["signature_text"]
    # playbook_directives must not leak as a top-level key.
    assert "playbook_directives" not in out


def test_generate_summary_playbook_without_signature():
    payload = _payload()
    payload.pop("condition_signature")
    payload.pop("condition_signature_text")
    client = _FakeClient(_GOOD_RESPONSE)
    out = generate_summary(race_name=None, payload=payload, client=client)
    assert out is not None
    assert "signature" not in out["playbook"]
    assert out["playbook"]["directives"]


def test_generate_summary_returns_none_on_api_error():
    client = _FakeClient(
        "ignored", raise_exc=RuntimeError("anthropic 429 rate limit"),
    )
    assert generate_summary(
        race_name="Test", payload=_payload(), client=client,
    ) is None


def test_generate_summary_returns_none_when_response_unparseable():
    client = _FakeClient("the model said no")
    assert generate_summary(
        race_name=None, payload=_payload(), client=client,
    ) is None


def test_generate_summary_returns_none_when_no_api_key(monkeypatch: pytest.MonkeyPatch):
    """No client passed AND no key in settings — should not raise."""
    fake_settings = type(
        "FakeSettings", (),
        {"anthropic_api_key": None, "anthropic_model": "x",
         "anthropic_analysis_model": "y"},
    )()

    import app.config
    monkeypatch.setattr(
        app.config, "get_settings", lambda: fake_settings, raising=False
    )
    assert generate_summary(race_name=None, payload=_payload()) is None


def test_prompt_version_is_4():
    assert PROMPT_VERSION == 4


def test_system_prompt_contains_hard_rules():
    sp = race_summary._SYSTEM_PROMPT
    assert "double-count" in sp
    assert "EXECUTION" in sp and "DECISION" in sp
    assert "do not comment on data that isn't present" in sp.lower()
