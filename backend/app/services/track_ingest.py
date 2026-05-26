"""Shared GPS-ingest side-effects used by both `/track` and `/telemetry`.

The two ingest endpoints originally diverged: `tracks.py` ran the mark-
rounding detector inline, persisted any new passes to
``race_sessions.mark_passes``, and triggered the ``race-postprocess``
Cloud Run Job when the final mark was crossed; `telemetry.py` did none
of that. Refactoring both routers to call this module keeps the
behaviour identical and prevents future drift between the two.

Four small functions, each independently mockable so the existing
test patterns (monkeypatch the trigger, assert UPDATE SQL) still work:

  * :func:`load_race_for_ingest` — auth-checked read of
    ``marks`` + ``mark_passes`` + ``started_at`` + ``start_at`` from
    the race row. 404 if the caller can't write to the race. Uses
    :func:`race_write_predicate` from ``auth_helpers`` so crew members
    can record on shared boats.

  * :func:`detect_and_persist_new_passes` — given a batch of GPS
    points (already translated to ``mark_rounding.Point``), run the
    detector resumed at the right index, UPDATE
    ``race_sessions.mark_passes`` if anything was found, and return
    ``(all_passes, new_passes)`` as plain dicts ready for the
    router's response model. ALSO writes ``started_at`` (if NULL) and
    ``ended_at`` (when the final pass closes the course) in the same
    UPDATE — one round-trip per batch.

  * :func:`maybe_trigger_postprocess` — fire the postprocess job iff
    THIS batch was the one that crossed the final mark. Lives outside
    any transaction; logs and swallows all errors via the underlying
    ``job_trigger.trigger_race_postprocess`` helper.

The orchestration (call all three in order) stays in each router so the
GPS INSERT statement — which differs slightly between endpoints — can
sit between the load and the side effects without an extra abstraction.

JSONB shape:

* ``marks``: list of ``{"name": str, "lat": float, "lon": float, ...}``.
  Extra keys are tolerated and round-tripped.
* ``mark_passes``: list of ``{"mark_index": int, "ts": iso8601 str,
  "lat": float, "lon": float}``. Same shape that ``tracks.py`` has
  written since 0008. The ``ts``/``lat``/``lon`` are now the
  closest-approach point during the traversal (v2 — 2026-05-26), not
  the exit point. Existing data with exit-point timestamps remains
  valid; downstream consumers don't care which point inside the radius
  the timestamp came from.

This module does NOT insert track points — that statement is router-
specific (the legacy ``/track`` payload differs from the locked
``/telemetry`` schema). The routers handle the INSERT and then call
into here for the side-effects only.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Iterable, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

from app.auth_helpers import race_write_predicate
from app.services.job_trigger import trigger_race_postprocess
from app.services.mark_rounding import (
    Mark as DetectorMark,
    MarkRoundingDetector,
    Point as DetectorPoint,
    radii_for_course,
)

log = logging.getLogger(__name__)


# --- Loaders ----------------------------------------------------------


async def load_race_for_ingest(
    conn: asyncpg.Connection, race_id: UUID, uid: str
) -> dict:
    """Fetch ``marks`` + ``mark_passes`` + ``started_at`` + ``start_at``
    for the race.

    Returns ``{"marks": list[dict], "mark_passes": list[dict],
    "started_at": Optional[datetime], "start_at": Optional[datetime]}``.

    The lifecycle columns are needed by :func:`detect_and_persist_new_passes`
    to decide whether to backfill ``started_at`` on the first telemetry
    batch (idempotent: only writes if currently NULL and ``start_at`` is
    populated).

    Raises ``HTTPException(404)`` if the race doesn't exist OR the
    caller can't write to it.

    Crew members at role IN ('owner', 'crew') count as writeable
    (matches the D3 sharing model). Viewers cannot record tracks.

    The JSONB columns may arrive as either plain Python objects
    (default — asyncpg's global JSONB codec converts at the boundary)
    or as raw strings/bytes (in pathological connection setups or
    older fixtures). Both shapes are handled.
    """
    pred = race_write_predicate(race_alias="r", uid_placeholder="$2")
    row = await conn.fetchrow(
        f"""
        SELECT r.marks, r.mark_passes, r.started_at, r.start_at
        FROM race_sessions r
        WHERE r.id = $1 AND {pred}
        """,
        race_id,
        uid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")

    marks_raw = row["marks"]
    if isinstance(marks_raw, (bytes, str)):
        marks = json.loads(marks_raw) if marks_raw else []
    else:
        marks = marks_raw or []

    passes_raw = row["mark_passes"]
    if isinstance(passes_raw, (bytes, str)):
        passes = json.loads(passes_raw) if passes_raw else []
    else:
        passes = passes_raw or []

    return {
        "marks": marks,
        "mark_passes": passes,
        "started_at": row["started_at"],
        "start_at": row["start_at"],
    }


# --- Detector + persistence ------------------------------------------


def _build_detector_marks(marks: list[dict]) -> list[DetectorMark]:
    """Translate the JSONB mark dicts to ``mark_rounding.Mark`` objects.

    Returns an empty list if any mark is malformed (missing lat/lon,
    wrong types) — matches the defensive behaviour the original
    tracks.py helper had against pre-Alembic mark rows.
    """
    out: list[DetectorMark] = []
    for m in marks:
        try:
            out.append(
                DetectorMark(lat=float(m["lat"]), lon=float(m["lon"]))
            )
        except (KeyError, TypeError, ValueError):
            return []
    return out


def _passes_to_dicts(
    passes: list, existing_count: int
) -> list[dict]:
    """Turn ``mark_rounding.MarkPass`` objects into the JSONB dict
    shape persisted on ``race_sessions.mark_passes``.

    ``existing_count`` is unused today but kept for callers who want
    to assert continuity (next index should equal ``existing_count``).
    """
    return [
        {
            "mark_index": p.mark_index,
            "ts": p.ts.isoformat(),
            "lat": p.lat,
            "lon": p.lon,
        }
        for p in passes
    ]


def _parse_iso(ts_str: str) -> datetime:
    """Parse an ISO-8601 timestamp string, tolerating a trailing Z."""
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    return datetime.fromisoformat(ts_str)


async def detect_and_persist_new_passes(
    conn: asyncpg.Connection,
    *,
    race_id: UUID,
    marks: list[dict],
    existing_passes: list[dict],
    new_points: Iterable[DetectorPoint],
    started_at: Optional[datetime] = None,
    start_at: Optional[datetime] = None,
) -> tuple[list[dict], list[dict]]:
    """Run the detector over a single batch, persist new passes, return
    ``(all_passes, new_passes)`` as plain JSONB-shaped dicts.

    ``new_points`` are ``mark_rounding.Point`` objects — the router is
    responsible for translating its wire payload (whose field names
    differ between ``/track`` and ``/telemetry``) into Points before
    calling here.

    ``started_at`` / ``start_at`` are the current values from the race
    row (read by :func:`load_race_for_ingest`). When ``started_at`` is
    NULL and ``start_at`` is set, this function writes
    ``started_at = start_at`` in the same UPDATE that persists new
    passes. Idempotent — only writes if currently NULL.

    Side effect: when at least one new pass is detected, OR
    ``started_at`` needs backfilling, OR the final pass just closed the
    course (``ended_at`` write), executes ONE UPDATE on
    ``race_sessions``. No UPDATE runs in the all-quiet case — keeps the
    hot path quiet for the common no-rounding case.

    Auth: the caller MUST have already gone through
    :func:`load_race_for_ingest` so the UPDATE-by-id below is safe.

    Per-mark radii: the detector is constructed with
    :func:`radii_for_course`, which gives the final mark the wider
    ``FINAL_MARK_RADIUS_M`` zone (75 m vs 50 m for intermediate
    marks). Centralised in mark_rounding.py so changing the policy
    doesn't require touching the router.
    """
    detector_marks = _build_detector_marks(marks)

    # Always evaluate the started_at backfill, even if the course has no
    # detectable marks — we still want the gun time recorded once
    # telemetry starts flowing.
    needs_started_at_write = started_at is None and start_at is not None

    if not detector_marks:
        if needs_started_at_write:
            await conn.execute(
                """
                UPDATE race_sessions
                SET started_at = $1,
                    updated_at = NOW()
                WHERE id = $2
                """,
                start_at,
                race_id,
            )
        return list(existing_passes), []

    next_idx = len(existing_passes)
    if next_idx >= len(detector_marks):
        # All marks already rounded; nothing more to detect. Still need
        # to handle the started_at backfill if applicable.
        if needs_started_at_write:
            await conn.execute(
                """
                UPDATE race_sessions
                SET started_at = $1,
                    updated_at = NOW()
                WHERE id = $2
                """,
                start_at,
                race_id,
            )
        return list(existing_passes), []

    det = MarkRoundingDetector(
        detector_marks,
        radius_m=radii_for_course(len(detector_marks)),
        next_mark_index=next_idx,
    )
    new_pass_objs = det.feed_batch(new_points)

    if not new_pass_objs:
        if needs_started_at_write:
            await conn.execute(
                """
                UPDATE race_sessions
                SET started_at = $1,
                    updated_at = NOW()
                WHERE id = $2
                """,
                start_at,
                race_id,
            )
        return list(existing_passes), []

    new_passes = _passes_to_dicts(new_pass_objs, next_idx)
    all_passes = list(existing_passes) + new_passes

    # Did THIS batch close the course? If so, set ended_at to the final
    # pass timestamp (closest-approach to the final mark). Detector
    # emits passes in course order, so the last entry in new_passes is
    # the latest. We rely on the existing "all_passes count == marks
    # count" gate (same one maybe_trigger_postprocess uses).
    final_pass_just_fired = len(all_passes) == len(detector_marks)
    final_pass_ts: Optional[datetime] = None
    if final_pass_just_fired:
        final_pass_ts = _parse_iso(new_passes[-1]["ts"])

    # Build the UPDATE dynamically so we only set the lifecycle columns
    # when they need it. mark_passes is always written when we got here
    # (new_pass_objs is non-empty by this point).
    set_parts: list[str] = ["mark_passes = $1::jsonb", "updated_at = NOW()"]
    args: list = [json.dumps(all_passes)]
    if needs_started_at_write:
        args.append(start_at)
        set_parts.append(f"started_at = ${len(args)}")
    if final_pass_ts is not None:
        # COALESCE so we never overwrite a manually-set ended_at (e.g.
        # a previous DNF PATCH). In practice these timestamps would be
        # close anyway, but keeping the column monotonic on write is
        # the right policy.
        args.append(final_pass_ts)
        set_parts.append(f"ended_at = COALESCE(ended_at, ${len(args)})")
    args.append(race_id)
    race_id_idx = len(args)

    await conn.execute(
        f"""
        UPDATE race_sessions
        SET {", ".join(set_parts)}
        WHERE id = ${race_id_idx}
        """,
        *args,
    )
    return all_passes, new_passes


# --- Postprocess trigger ---------------------------------------------


async def maybe_trigger_postprocess(
    race_id: UUID,
    marks: list[dict],
    all_passes: list[dict],
    new_passes: list[dict],
) -> bool:
    """Fire the ``race-postprocess`` Cloud Run Job iff THIS batch caused
    the final mark to be crossed.

    Returns True if the trigger was actually fired (useful for tests
    asserting on the trigger). Never raises — the underlying
    :func:`trigger_race_postprocess` is itself fully tolerant of every
    failure mode (missing env var, no ADC, network error).

    Conditions:
      * at least one new pass landed in this batch (otherwise a re-flush
        of an old completed race would re-fire the job), AND
      * the course actually has marks (defensive — should never be 0
        in production), AND
      * the cumulative pass count now equals the course length.

    Deliberately fires AFTER the DB UPDATE so a job failure can't
    rollback the pass persistence.
    """
    total_marks = len(marks or [])
    if not new_passes:
        return False
    if total_marks == 0:
        return False
    if len(all_passes) != total_marks:
        return False
    log.info(
        "track_ingest: race %s final mark rounded, kicking off postprocess",
        race_id,
    )
    await trigger_race_postprocess(race_id)
    return True
