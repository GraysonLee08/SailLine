"""Per-evaluation trace for the tactician pipeline (Phase A, 2026-07-09).

The pipeline has ~10 sequential silent exit points; before this module
the only instrument was unstructured Cloud Run log lines. ``EvalTrace``
accumulates facts as ``_evaluate`` progresses and, at every exit,
writes exactly ONE compact JSON record to a per-race Redis ring buffer
(``tactics:trace:{race_id}`` — LPUSH + LTRIM + EXPIRE), newest first.
``GET /api/races/{id}/tactics/debug`` reads it back so "why was the
cockpit quiet?" is answerable from a phone, mid-race.

Design constraints, in priority order:

1. **Never raise.** The trace shares the pipeline's defensive posture —
   a tracing bug must degrade to "no trace", not break an evaluation
   (which itself must never break telemetry ingest).
2. **One record per evaluation**, including the earliest gate exits —
   an empty buffer after a race means the task never spawned (tier
   gate / no fresh GPS), which is itself the diagnosis.
3. **Compact.** Records are capped by construction (decimated context,
   no track dumps): the 200-entry ring stays ~100 KB worst case.

Exit reasons (pinned by tests — add here AND to the test when a new
exit is instrumented):

  cooldown_global | race_not_found | race_ended | not_live | opted_out
  | insufficient_track | no_forecast | no_candidates | cooldown_type
  | advisor_silent_or_failed | dropped_late | published | error
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional, Union
from uuid import UUID

log = logging.getLogger(__name__)

# Ring buffer length. At the 180 s global-cooldown floor a race night
# produces ~20 evaluations/hour; 200 comfortably holds several sessions
# inside the 24 h TTL.
TRACE_MAX_ENTRIES: int = 200

EXIT_REASONS: frozenset[str] = frozenset({
    "cooldown_global",
    "race_not_found",
    "race_ended",
    "not_live",
    "opted_out",
    "insufficient_track",
    "no_forecast",
    "no_candidates",
    "cooldown_type",
    "advisor_silent_or_failed",
    "dropped_late",
    "published",
    "error",
})


def _json_default(value: Any) -> str:
    """Serialize datetimes/UUIDs; last-resort str() for anything odd."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


class EvalTrace:
    """Accumulator for one pipeline evaluation.

    Usage (pipeline)::

        trace = EvalTrace(race_id, now=now)
        ...
        trace.gate("forecast", False)
        return await trace.finish(redis, "no_forecast")

    ``finish`` is idempotent-enough for the pipeline's single-exit
    control flow (each code path calls it exactly once) and returns the
    record so the caller can also emit it as a structured log line.
    """

    def __init__(self, race_id: Union[UUID, str], *, now: datetime) -> None:
        self._t0 = time.monotonic()
        self._race_id = race_id
        self._record: dict[str, Any] = {
            "t": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "exit": None,
            "elapsed_ms": None,
            "gates": {},
            "context": None,
            "candidates": [],
            "call": None,
        }

    # ── accumulation ────────────────────────────────────────────────

    def gate(self, name: str, value: Any) -> None:
        """Record a gate observation (count, bool, or short string)."""
        self._record["gates"][name] = value

    def set_context(
        self,
        *,
        evals: Optional[list[dict]] = None,
        heel_stat: Optional[dict] = None,
        next_mark: Optional[dict] = None,
        mark_dist_nm: Optional[float] = None,
    ) -> None:
        """Compact performance summary from data the pipeline already
        computed. Uses the LAST eval (freshest fix) — near-miss numbers
        for the coaching detectors (pinching / off-pace / over-heel)
        are readable from every trace even when nothing fired."""
        ctx: dict[str, Any] = {}
        if evals:
            last = evals[-1]
            ctx.update({
                "sog_kts": last.get("actual_kts"),
                "target_kts": last.get("target_kts"),
                "speed_ratio": last.get("speed_ratio"),
                "twa_deg": last.get("twa"),
                "tws_kts": last.get("tws_kts"),
                "vmg_ratio": last.get("vmg_ratio"),
                "eval_count": len(evals),
            })
        if heel_stat:
            ctx["heel_median_abs_deg"] = heel_stat.get("median_abs_deg")
            ctx["heel_mount_ok"] = heel_stat.get("mount_ok")
        if next_mark:
            ctx["next_mark"] = next_mark.get("label")
        if mark_dist_nm is not None:
            ctx["mark_dist_nm"] = round(mark_dist_nm, 2)
        self._record["context"] = ctx or None

    def set_candidates(self, candidates: list, winner=None) -> None:
        """Detector outcomes: every fired candidate + which won the
        per-type cooldown race. ``diagnosis`` is included — it carries
        the detector's numbers (threshold tuning reads these)."""
        self._record["candidates"] = [
            {
                "type": c.call_type,
                "class": c.call_class,
                # Serialized here (not left to json.dumps) so the
                # returned record and the stored blob are identical.
                "eta": _json_default(c.eta) if c.eta is not None else None,
                "diagnosis": c.diagnosis,
                "won": c is winner,
            }
            for c in candidates
        ]

    def set_call(self, *, message: str, model: str) -> None:
        self._record["call"] = {"message": message, "model": model}

    # ── terminal write ──────────────────────────────────────────────

    async def finish(self, redis, exit_reason: str) -> Optional[dict]:
        """Seal the record and write it to the ring buffer. Never
        raises; returns the record (or None if sealing itself failed,
        which should be impossible)."""
        try:
            if exit_reason not in EXIT_REASONS:
                # Tolerate an unpinned reason rather than losing the
                # trace — the test suite keeps this from shipping.
                log.warning("tactician trace: unknown exit %r", exit_reason)
            self._record["exit"] = exit_reason
            self._record["elapsed_ms"] = int(
                (time.monotonic() - self._t0) * 1000
            )
            blob = json.dumps(self._record, default=_json_default)
        except Exception:  # noqa: BLE001
            log.exception("tactician trace: failed to build record")
            return None

        try:
            from app.services.redis_keys import (
                TACTICS_TRACE_TTL_S,
                tactics_trace_key,
            )
            key = tactics_trace_key(self._race_id)
            pipe = redis.pipeline()
            pipe.lpush(key, blob.encode())
            pipe.ltrim(key, 0, TRACE_MAX_ENTRIES - 1)
            pipe.expire(key, TACTICS_TRACE_TTL_S)
            await pipe.execute()
        except Exception:  # noqa: BLE001
            log.exception(
                "tactician trace: redis write failed race=%s", self._race_id
            )
        return self._record
