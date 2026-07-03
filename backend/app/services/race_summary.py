"""Generate the post-race analysis via Claude (prompt v4).

Pure I/O wrapper around the Anthropic SDK. Pure-function helpers
(``build_prompt``, ``parse_response``) are exported so tests can
exercise them without needing API credentials.

v4 replaces the recap+tips coach note with a quantitative race
analysis over the derived-metrics payload built by
``app.services.race_analysis`` (spec: 2026-07-03). Shape persisted to
``race_sessions.ai_summary``::

    {
        "summary":       "string — ≤5 sentence race story",
        "what_worked":   ["string", ...],            # 3-5, evidence-cited
        "what_cost":     [                            # ranked by cost
            {"tag": "EXECUTION"|"DECISION",
             "text": "string", "cost_s": float|null},
        ],
        "total_identifiable_loss_s": float|null,
        "playbook": {
            "signature":      {...},   # computed by signature.py, NOT the model
            "signature_text": "TWS 8-12 kt, TWD ~220° with oscillating ±12°",
            "directives":     ["string", ...],       # 4-7, testable
        },
        "analysis":       {...},   # the full derived-metrics payload
        "model":          "claude-sonnet-4-6",
        "prompt_version": 4,
        "generated_at":   "2026-07-03T18:30:00Z",
    }

The derived payload is persisted alongside the model output so the
review UI can render start/leg/maneuver numbers without recompute and
the pre-race matcher can read ``playbook.signature`` directly.

If the Anthropic call fails (no key, network, rate limit, malformed
response), this module returns ``None`` rather than raising. Callers
(the Cloud Run Job, the stats endpoint) treat None as "no summary
yet" and degrade gracefully — the user can hit Regenerate.

PROMPT_VERSION
--------------
Bump when you change the prompt template, the model, or the
input-shape contract. The Cloud Run Job compares the stored
``prompt_version`` against this constant and regenerates on mismatch.
NOTE: v3 → v4 changed the output shape entirely; the web/mobile
renderers tolerate the legacy ``{recap, tips}`` shape until a race is
regenerated.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


# v4 — full quantitative analysis over the race_analysis payload
# (derived metrics, four-section output, condition-signature playbook).
# v3 — recap+tips with heel summary (legacy shape).
PROMPT_VERSION: int = 4

# Default model — the config has the override knob
# (ANTHROPIC_ANALYSIS_MODEL). Sonnet class: this is synthesis over
# server-computed numbers, heavier than the tactician's rephrasing job
# but deliberately NOT extended thinking (the Python does the math).
_DEFAULT_MODEL = "claude-sonnet-4-6"

# Output ceiling. Four sections with 4-7 directives lands well inside;
# headroom for a messy race with many findings.
_MAX_TOKENS = 3000


# ─── Prompt template ───────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You are an expert sailing race coach and tactician analyzing a \
completed race for a single boat. You are direct, specific, and \
quantitative. You never pad, never generically praise, and never give \
advice that isn't tied to a number or event in the data.

Rules:
- Every observation must cite specific evidence: a leg number, \
timestamp, metric value, or event from the payload.
- Distinguish between execution errors (crew/helm controllable: slow \
tacks, pinching, poor start timing) and decision errors (strategy: \
wrong side, missed shifts, overstands). Label each finding EXECUTION \
or DECISION via the "tag" field.
- Quantify cost wherever possible, in seconds or boatlengths. Rank \
findings by cost, largest first. Do not double-count overlapping \
losses (a missed shift and low VMG on the same stretch are the same \
loss — count it once).
- If data is ambiguous or a metric could have an innocent explanation \
(e.g., traffic forcing an overstand), say so rather than asserting.
- Do not comment on data that isn't present. No advice about sail \
selection, crew work details, or competitors — that data is not \
collected.
- The start analysis labels the line ends "A" and "B" relative to the \
line bearing — the data cannot tell which physical end was the pin, \
so never claim to know.
- When the tactician_calls section shows a call the boat did not \
respond to, note it neutrally as a review item, not a criticism.
- Speak to the sailor as "you." Assume they are experienced; skip \
basics.

Output STRICT JSON of exactly this shape — no markdown, no code \
fences, no keys beyond these:

{
  "summary": "string — <=5 sentences: conditions signature, course, headline result, and the single biggest story of the race",
  "what_worked": ["string", ...],
  "what_cost": [
    {"tag": "EXECUTION", "text": "string", "cost_s": 30.0},
    {"tag": "DECISION",  "text": "string", "cost_s": null}
  ],
  "total_identifiable_loss_s": 120.0,
  "playbook_directives": ["string", ...]
}

Section requirements:
- what_worked: 3-5 findings, each with evidence and estimated \
benefit. Only include things genuinely above par (e.g., \
vmg_efficiency > 0.95, tack loss below fleet-typical ~2 boatlengths, \
start within 1 boatlength of the line at the gun). Fewer than 3 is \
fine if the data doesn't support more.
- what_cost: ranked by estimated cost in seconds, largest first. Each \
entry: tag, evidence (leg/time/metric), cost estimate in cost_s \
(null when honestly unquantifiable), and a root-cause hypothesis in \
the text. total_identifiable_loss_s = the sum of the quantified \
cost_s values; compare it to the corrected-time margin in the summary \
if result data allows.
- playbook_directives: 4-7 numbered-free, testable directives for the \
next race in this condition signature. Each must be specific enough \
to execute without interpretation (e.g., "tack on headers >=8 deg \
that persist 60s; ignore smaller oscillations — you tacked 14 times \
on leg 1 and 5 were on <5 deg blips costing ~11 boatlengths"). \
Include one directive about start execution and one about maneuver \
economy if the data supports them.

Do not echo the input data. Do not apologise for anything.\
"""


def build_prompt(payload: dict) -> str:
    """Render the user message. Pure function — ``payload`` is the dict
    from ``race_analysis.build_race_analysis`` plus the race name the
    worker injects."""
    return (
        "Analyze this race. Payload follows.\n\n<race_data>\n"
        + json.dumps(payload, indent=1, default=str)
        + "\n</race_data>"
    )


# ─── Response parsing ─────────────────────────────────────────────────


def parse_response(raw_text: str) -> Optional[dict]:
    """Pull the analysis object out of the model's reply.

    Forgiving of code fences / prepended prose: finds the first ``{``
    and the last ``}`` and json-loads the slice. Returns None when the
    result doesn't match the required shape — caller treats that as
    "summary unavailable".
    """
    if not raw_text:
        return None
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start < 0 or end <= start:
        log.warning("ai analysis: no JSON object found in response")
        return None
    try:
        obj = json.loads(raw_text[start : end + 1])
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("ai analysis: failed to parse JSON (%s)", e)
        return None
    if not isinstance(obj, dict):
        return None

    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        log.warning("ai analysis: missing/empty summary: %r", obj)
        return None

    what_worked = [
        w for w in (obj.get("what_worked") or []) if isinstance(w, str)
    ]

    what_cost: list[dict] = []
    for item in obj.get("what_cost") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        tag = item.get("tag")
        if tag not in ("EXECUTION", "DECISION"):
            tag = "DECISION"
        cost = item.get("cost_s")
        what_cost.append({
            "tag": tag,
            "text": text.strip(),
            "cost_s": float(cost) if isinstance(cost, (int, float)) else None,
        })

    total = obj.get("total_identifiable_loss_s")
    directives = [
        d for d in (obj.get("playbook_directives") or []) if isinstance(d, str)
    ]

    return {
        "summary": summary.strip(),
        "what_worked": what_worked,
        "what_cost": what_cost,
        "total_identifiable_loss_s": (
            float(total) if isinstance(total, (int, float)) else None
        ),
        "playbook_directives": directives,
    }


# ─── Anthropic SDK bridge ─────────────────────────────────────────────


def _build_client(api_key: str) -> Any:
    """Lazy SDK import — keeps app startup fast and means tests don't
    need the sdk installed if they only exercise the pure helpers."""
    from anthropic import Anthropic  # type: ignore[import-not-found]
    return Anthropic(api_key=api_key)


def generate_summary(
    *,
    race_name: Optional[str],
    payload: dict,
    client: Any = None,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Call Claude over the derived-metrics payload; return the stored
    ai_summary dict. Returns None on any failure — callers never see
    exceptions from this layer.

    ``payload`` is ``race_analysis.build_race_analysis`` output. The
    computed condition signature is attached to the playbook here (the
    model only writes directives — it never invents the signature).
    """
    if client is None:
        try:
            from app.config import get_settings
            settings = get_settings()
            mdl = (
                model
                or getattr(settings, "anthropic_analysis_model", None)
                or _DEFAULT_MODEL
            )
            key = settings.anthropic_api_key
        except Exception as e:  # noqa: BLE001
            log.warning("ai analysis: settings unavailable (%s)", e)
            return None
        if not key:
            log.info("ai analysis: ANTHROPIC_API_KEY not set, skipping")
            return None
        try:
            client = _build_client(key)
        except Exception as e:  # noqa: BLE001 - SDK import-time issues
            log.warning("ai analysis: failed to build Anthropic client (%s)", e)
            return None
    else:
        mdl = model or _DEFAULT_MODEL

    prompt_payload = {"race_name": race_name or "Untitled race", **payload}

    try:
        msg = client.messages.create(
            model=mdl,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(prompt_payload)}],
        )
    except Exception as e:  # noqa: BLE001 - network/auth/rate-limit
        log.warning("ai analysis: Anthropic call failed (%s)", e)
        return None

    raw_text = _extract_text(msg)
    parsed = parse_response(raw_text)
    if parsed is None:
        return None

    playbook: dict = {
        "directives": parsed.pop("playbook_directives"),
    }
    sig = payload.get("condition_signature")
    if sig:
        playbook["signature"] = sig
        playbook["signature_text"] = payload.get("condition_signature_text")

    return {
        **parsed,
        "playbook": playbook,
        "analysis": payload,
        "model": mdl,
        "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _extract_text(msg: Any) -> str:
    """Pull the text out of an Anthropic Message response.

    The SDK shape is ``Message.content -> list[ContentBlock]``. We
    concatenate every ``text`` block, in order. Defensive against
    older/newer SDK shapes so a minor SDK bump doesn't take stats
    down.
    """
    try:
        blocks = getattr(msg, "content", None) or []
        out = []
        for b in blocks:
            t = getattr(b, "text", None)
            if isinstance(t, str):
                out.append(t)
            elif isinstance(b, dict) and isinstance(b.get("text"), str):
                out.append(b["text"])
        return "".join(out)
    except Exception:  # noqa: BLE001
        return ""
