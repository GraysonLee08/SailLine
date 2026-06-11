"""In-race AI tactician — event-gated, Claude-phrased tactical calls.

Spec: ``sailline -docs/2026-06-11_ai-tactician-spec.md``.

Module map (mirrors ``services/routing/``):

* ``detectors``  — pure call-candidate detectors (no I/O)
* ``heel``       — sustained-heel statistic + mount-quality gate
* ``heel_bands`` — per-boat-class optimal-heel + trim rule tables
* ``snapshot``   — tactical-snapshot builder for the advisor prompt
* ``advisor``    — Anthropic bridge (PROMPT_VERSION'd, SILENT-aware)
* ``pipeline``   — orchestration: context load → detect → advise → publish

The only entry point callers should need:

    from app.services.tactics.pipeline import evaluate_tactics_safe
"""
