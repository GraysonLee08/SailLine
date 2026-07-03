"""Post-race derived-metrics engine (spec: 2026-07-03 analysis upgrade).

Pure functions over data the postprocess worker loads — no I/O
anywhere in this package. ``payload.build_race_analysis`` is the
orchestrating entry point; the individual sections live in their own
modules (start, legs, maneuvers, shifts, laylines, roundings, leeway,
wind_timeline, calls) and are unit-tested independently.

``signature`` is shared with the pre-race playbook matcher in the
tactician pipeline — post-race writes the condition signature, the
matcher scores today's forecast against it.
"""
from app.services.race_analysis.payload import build_race_analysis
from app.services.race_analysis.signature import (
    best_match,
    match_score,
    signature_text,
)

__all__ = [
    "build_race_analysis",
    "best_match",
    "match_score",
    "signature_text",
]
