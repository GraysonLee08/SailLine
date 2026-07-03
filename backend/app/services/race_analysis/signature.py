"""Condition signatures — the key that links post-race playbooks to
pre-race forecasts.

A signature is the structured output of
``wind_timeline.summarize_conditions`` plus optional sea state /
current:

    {
      "tws_lo_kts": 8.0, "tws_hi_kts": 12.5,
      "twd_mean_deg": 220.0,
      "character": "oscillating" | "persistent_right" | "persistent_left" | "steady",
      "osc_amplitude_deg": 12.0 | None,
      "tws_trend": "building" | "dying" | "steady",
    }

``match_score`` grades how well two signatures agree in [0, 1]:
TWS band IoU is weighted heaviest (playbook directives are mostly
pressure-dependent), then shift character, then TWD proximity (venue
geography — a NW playbook on a SW day is suspect on a lake). The
pre-race matcher uses MATCH_THRESHOLD.

Deterministic Python end to end — the model never invents a signature;
it's computed here and attached to the stored playbook.
"""
from __future__ import annotations

from typing import Optional

from app.services.race_analysis.geo import angle_diff

MATCH_THRESHOLD = 0.6

_W_TWS = 0.5
_W_CHARACTER = 0.35
_W_TWD = 0.15

# Characters that are "close enough" to score half marks.
_NEAR_CHARACTERS = {
    frozenset({"persistent_right", "persistent_left"}),
    frozenset({"oscillating", "steady"}),
}


def signature_text(sig: dict) -> str:
    """Human-readable one-liner in the playbook's required form."""
    lo = sig.get("tws_lo_kts")
    hi = sig.get("tws_hi_kts")
    twd = sig.get("twd_mean_deg")
    character = sig.get("character") or "steady"
    if character == "oscillating":
        amp = sig.get("osc_amplitude_deg")
        char_txt = f"oscillating ±{amp:.0f}°" if amp else "oscillating"
    elif character.startswith("persistent"):
        char_txt = f"persistent {character.split('_')[1]} trend"
    else:
        char_txt = "steady"
    trend = sig.get("tws_trend")
    trend_txt = f", {trend}" if trend in ("building", "dying") else ""
    parts = []
    if lo is not None and hi is not None:
        parts.append(f"TWS {lo:.0f}-{hi:.0f} kt")
    if twd is not None:
        parts.append(f"TWD ~{twd:.0f}° with {char_txt}{trend_txt}")
    return ", ".join(parts) if parts else "conditions unknown"


def _tws_overlap(a: dict, b: dict) -> Optional[float]:
    try:
        a_lo, a_hi = float(a["tws_lo_kts"]), float(a["tws_hi_kts"])
        b_lo, b_hi = float(b["tws_lo_kts"]), float(b["tws_hi_kts"])
    except (KeyError, TypeError, ValueError):
        return None
    # Pad each band by 1 kt — a 10-12 playbook is fine on an 8-10 day-ish.
    a_lo, a_hi = a_lo - 1.0, a_hi + 1.0
    b_lo, b_hi = b_lo - 1.0, b_hi + 1.0
    inter = min(a_hi, b_hi) - max(a_lo, b_lo)
    union = max(a_hi, b_hi) - min(a_lo, b_lo)
    if union <= 0:
        return None
    return max(0.0, inter) / union


def match_score(a: Optional[dict], b: Optional[dict]) -> float:
    """0..1 agreement between two signatures. 0 when either is missing."""
    if not a or not b:
        return 0.0
    score = 0.0

    tws = _tws_overlap(a, b)
    if tws is not None:
        score += _W_TWS * tws

    ca, cb = a.get("character"), b.get("character")
    if ca and cb:
        if ca == cb:
            score += _W_CHARACTER
        elif frozenset({ca, cb}) in _NEAR_CHARACTERS:
            score += _W_CHARACTER * 0.5

    try:
        twd_off = abs(angle_diff(float(a["twd_mean_deg"]), float(b["twd_mean_deg"])))
        score += _W_TWD * max(0.0, 1.0 - twd_off / 90.0)
    except (KeyError, TypeError, ValueError):
        pass

    return round(score, 3)


def best_match(
    today: Optional[dict],
    candidates: list[dict],
) -> Optional[dict]:
    """Pick the highest-scoring past playbook above MATCH_THRESHOLD.

    ``candidates`` is ``[{signature, directives, race_id?, race_name?,
    generated_at?}, ...]``. Returns the winning candidate dict with
    ``score`` added, or None.
    """
    best: Optional[dict] = None
    best_score = MATCH_THRESHOLD
    for c in candidates:
        s = match_score(today, c.get("signature"))
        if s >= best_score:
            best = {**c, "score": s}
            best_score = s
    return best


__all__ = ["signature_text", "match_score", "best_match", "MATCH_THRESHOLD"]
