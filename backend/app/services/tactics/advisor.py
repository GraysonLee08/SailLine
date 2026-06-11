"""Anthropic bridge for the in-race tactician.

Mirrors ``race_summary.py``'s structure: pure helpers exported for
tests, lazy SDK import, injectable client, returns ``None`` on every
failure mode (no key, network, malformed output) — callers never see
exceptions from this layer. A failed advisor call means a skipped
call, never a crashed telemetry request.

SILENT contract
---------------
The system prompt instructs the model to reply with exactly ``SILENT``
when the snapshot doesn't justify interrupting the crew. ``parse_response``
maps that (and anything unusable) to ``None``. This is the safety valve
that lets detector thresholds stay forgiving.

PROMPT_VERSION
--------------
Bump on any change to the system prompt, model, or snapshot shape.
Stored on every ``tactician_calls`` row so on-water tuning sessions can
correlate call quality with the prompt that produced it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

PROMPT_VERSION: int = 1

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# One short sentence out; tiny budget keeps p99 latency low.
_MAX_TOKENS = 120

MAX_CALL_CHARS = 140


_SYSTEM_PROMPT = """\
You are the onboard tactician on a racing sailboat, speaking one short \
call to the crew over the noise. You receive a JSON snapshot: the \
server's detectors found something ("trigger"), plus recent \
performance, wind now and ahead at the boat, the next mark, and the \
last calls already made.

Rules — these are hard constraints:
* Reply with ONE sentence, max 140 characters. No preamble, no \
markdown, no quotes around it.
* If the trigger is a maneuver (it has "seconds_until_event"), you \
MUST state the time horizon in natural words ("in about 2 minutes", \
"in 90 seconds"). Never give a bare imperative with no time.
* If the trigger is coaching (no eta), name ONE specific adjustment \
taken from "candidate_adjustments". Never just describe the symptom.
* Use only numbers present in the snapshot. Never invent wind, \
distances, times, or angles.
* If a recent call already said this and nothing material changed, or \
the situation doesn't justify interrupting a busy crew, reply with \
exactly: SILENT
* Calm race-coach tone. No exclamation marks. No apologies.\
"""


# ─── Pure helpers (test these without the SDK) ──────────────────────────


def build_prompt(snapshot: dict) -> str:
    """User-message body: the snapshot, pretty enough to read in logs."""
    return json.dumps(snapshot, indent=1, default=str)


def parse_response(raw_text: Optional[str]) -> Optional[str]:
    """Model output → call text, or None for SILENT / unusable.

    Tolerant of whitespace and stray quoting; hard-caps length at
    MAX_CALL_CHARS (a model that ignored the limit gets truncated at
    the last word boundary rather than dropped — an overlong good call
    beats no call).
    """
    if not raw_text:
        return None
    text = raw_text.strip().strip('"').strip()
    if not text:
        return None
    if text.upper().startswith("SILENT"):
        return None
    # Collapse internal newlines — it renders as a one-line notification.
    text = " ".join(text.split())
    if len(text) > MAX_CALL_CHARS:
        cut = text[:MAX_CALL_CHARS]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        text = cut
    return text


# ─── Anthropic SDK bridge ────────────────────────────────────────────────


def _build_client(api_key: str) -> Any:
    """Lazy SDK import — same pattern as ``race_summary._build_client``."""
    from anthropic import Anthropic  # type: ignore[import-not-found]
    return Anthropic(api_key=api_key)


def generate_call(
    snapshot: dict,
    *,
    client: Any = None,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Call Claude over the snapshot; return the call record or None.

    Synchronous (the SDK client is sync, matching race_summary) — the
    async pipeline runs this via ``asyncio.to_thread`` so the event
    loop is never blocked.

    Return shape (persisted to ``tactician_calls``)::

        {
          "message":        str,   # ≤140 chars, the call itself
          "model":          str,
          "prompt_version": int,
          "generated_at":   str,   # ISO 8601 UTC
        }
    """
    if client is None:
        try:
            from app.config import get_settings
            settings = get_settings()
            mdl = model or settings.anthropic_model or _DEFAULT_MODEL
            key = settings.anthropic_api_key
        except Exception as e:  # noqa: BLE001
            log.warning("tactician: settings unavailable (%s)", e)
            return None
        if not key:
            log.info("tactician: no ANTHROPIC_API_KEY — skipping call")
            return None
        try:
            client = _build_client(key)
        except Exception as e:  # noqa: BLE001
            log.warning("tactician: client build failed (%s)", e)
            return None
    else:
        mdl = model or _DEFAULT_MODEL

    try:
        resp = client.messages.create(
            model=mdl,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(snapshot)}],
        )
        raw = "".join(
            block.text for block in resp.content
            if getattr(block, "type", None) == "text"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("tactician: API call failed (%s)", e)
        return None

    message = parse_response(raw)
    if message is None:
        log.info("tactician: model returned SILENT/unusable — no call")
        return None
    return {
        "message": message,
        "model": mdl,
        "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
