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
from datetime import datetime, timezone
from typing import Iterable, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

from app.auth_helpers import race_write_predicate
from app.services.job_trigger import trigger_race_postprocess
from app.services.mark_gates import GateAwareDetector, build_gates
from app.services.mark_rounding import (
    Mark as DetectorMark,
    Point as DetectorPoint,
    clamp_thresholds_to_spacing,
    thresholds_for_course,
)
from app.services.start_line import resolve_start_line_bearing

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

    Concurrency (added 2026-06-01): the SELECT uses ``FOR UPDATE`` to
    serialize concurrent batches against the same race row. The
    detection-and-update logic in :func:`detect_and_persist_new_passes`
    is a read-modify-write on ``race_sessions.mark_passes`` (read
    existing_passes, run detector starting at len(existing_passes),
    UPDATE with the full new list). Without the row lock two
    overlapping batches — common once the recorder's durable queue
    starts retrying — would each read the same starting state, each
    detect from the same index, and the second UPDATE would clobber
    the first.

    The ``FOR UPDATE`` is acquired inside the caller's transaction
    (both /track and /telemetry wrap their work in
    ``conn.transaction()``). The lock holds until that transaction
    commits or rolls back. Cost is negligible — we never have more
    than a handful of concurrent batches per race, and the held
    duration is the single bulk INSERT plus one short UPDATE.
    """
    pred = race_write_predicate(race_alias="r", uid_placeholder="$2")
    row = await conn.fetchrow(
        f"""
        SELECT r.marks, r.mark_passes, r.started_at, r.start_at, r.mode,
               r.detector_state,
               r.start_line_bearing_override, r.start_line_bearing_deg
        FROM race_sessions r
        WHERE r.id = $1 AND {pred}
        FOR UPDATE OF r
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

    # detector_state: JSONB column added in 0020. NULL is the "fresh
    # traversal" sentinel; restore_state treats both None and {} as
    # no-op. Same dual-shape tolerance as marks/mark_passes — asyncpg
    # global JSONB codec usually decodes for us but older connection
    # setups occasionally hand back the raw string.
    detector_state_raw = row["detector_state"]
    if isinstance(detector_state_raw, (bytes, str)):
        detector_state = (
            json.loads(detector_state_raw) if detector_state_raw else None
        )
    else:
        detector_state = detector_state_raw

    return {
        "marks": marks,
        "mark_passes": passes,
        "started_at": row["started_at"],
        "start_at": row["start_at"],
        # v3 mark-detection thresholds vary by mode. Default to "distance"
        # (the wider tolerance) when the column is somehow NULL.
        "mode": row["mode"] or "distance",
        "detector_state": detector_state,
        # v4 start/finish line bearing (0022). Override = user-entered;
        # deg = system-resolved cache. Both may be NULL — the detector
        # falls back to CPA for the start/finish marks until resolved.
        "start_line_bearing_override": row["start_line_bearing_override"],
        "start_line_bearing_deg": row["start_line_bearing_deg"],
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


def _as_utc(dt: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC.

    DB timestamptz values arrive aware; wire timestamps should too, but
    a naive datetime slipping through a fixture or an older client must
    not blow up the pre-start comparison. Naive is assumed UTC — the
    only convention this codebase uses.
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def detect_and_persist_new_passes(
    conn: asyncpg.Connection,
    *,
    race_id: UUID,
    marks: list[dict],
    existing_passes: list[dict],
    new_points: Iterable[DetectorPoint],
    started_at: Optional[datetime] = None,
    start_at: Optional[datetime] = None,
    mode: str = "distance",
    detector_state: Optional[dict] = None,
    start_line_bearing_override: Optional[float] = None,
    start_line_bearing_deg: Optional[float] = None,
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

    ``detector_state`` (added 2026-06-04) is the JSONB snapshot of the
    previous batch's traversal state from
    ``race_sessions.detector_state``. ``None`` means "fresh traversal"
    — required for the first batch of a race and any batch following
    a pass emit. With cross-batch state persistence the detector can
    accumulate the depart-confirm count across small batches; without
    it, batches containing ≤ 3 samples would never trigger an emit
    because ``_last_dist`` resets to None on every call.

    Side effect: every call writes ``detector_state`` (even on quiet
    batches) so the next batch can resume. The other lifecycle columns
    are conditionally appended to the same UPDATE: ``mark_passes``
    when new passes detected, ``started_at`` when backfilling, and
    ``ended_at`` when the final pass closes the course. Before 0020
    the quiet case ran no UPDATE; now it always runs one. The cost is
    ~negligible (single row, small JSONB) compared to the depart-
    confirm bug it fixes.

    Auth: the caller MUST have already gone through
    :func:`load_race_for_ingest` so the UPDATE-by-id below is safe.

    Per-mark thresholds: the detector is constructed with
    :func:`thresholds_for_course(n, mode)`. Distance-mode races get
    400 m (intermediate) / 450 m (final), inshore-mode races get
    100 m / 150 m. The wider distance thresholds are required for
    races past passage marks the boat doesn't actually round (cribs,
    met buoys) — see ``sailline-docs/2026-05-30_session.md`` for the
    Colors (Bravo) post-mortem that motivated v3.
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
        # to handle the started_at backfill if applicable. Also clear
        # any lingering detector_state — the course is closed.
        await _persist_quiet_state(
            conn,
            race_id=race_id,
            new_detector_state=None,
            backfill_start_at=start_at if needs_started_at_write else None,
        )
        return list(existing_passes), []

    # ── Pre-start filtering (added 2026-07-02) ──────────────────────
    # While the detector is still watching the FIRST mark and the race
    # has a scheduled gun (start_at), samples recorded before the gun
    # must not participate in detection. The Beer Can 7.1.2026 race
    # showed why: a 390 m closest approach recorded two minutes BEFORE
    # the start became the persisted running minimum; the genuine start
    # crossing then read as "approaching again" (departing counter
    # reset on every new minimum) and the pass never emitted — which
    # cascaded into every subsequent mark reading as "missed" and
    # auto-finish never firing. Boats legitimately mill around the
    # start area pre-gun; none of that motion is course progress.
    #
    # Two parts:
    #   1. Drop batch samples timestamped before the gun.
    #   2. Discard persisted detector_state whose CPA (min_ts) predates
    #      the gun — or whose provenance can't be verified (no min_ts).
    #      A discarded state costs at most a couple of depart-confirm
    #      samples; a poisoned one costs the whole race.
    # Marks past the first need no filter: the detector can only reach
    # them after a legitimate post-gun start pass.
    if start_at is not None and next_idx == 0:
        gun = _as_utc(start_at)
        new_points = [p for p in new_points if _as_utc(p.ts) >= gun]
        if detector_state:
            raw_min_ts = detector_state.get("min_ts")
            state_ts = (
                _parse_iso(raw_min_ts)
                if isinstance(raw_min_ts, str)
                else None
            )
            if state_ts is None or _as_utc(state_ts) < gun:
                detector_state = None
    else:
        new_points = list(new_points)

    # ── v4 gate detection (added 2026-07-02) ────────────────────────
    # Resolve the start/finish line bearing when the detector is
    # watching either line mark. Precedence: user override → cached →
    # forecast (resolved once, persisted, throttled on failure). None
    # simply degrades those marks to v3 CPA.
    line_bearing: Optional[float] = None
    if next_idx == 0 or next_idx == len(detector_marks) - 1:
        line_bearing = await resolve_start_line_bearing(
            conn,
            race_id=race_id,
            marks=marks,
            start_at=start_at,
            override=start_line_bearing_override,
            cached=start_line_bearing_deg,
        )
    elif start_line_bearing_override is not None:
        line_bearing = float(start_line_bearing_override)
    elif start_line_bearing_deg is not None:
        line_bearing = float(start_line_bearing_deg)

    # Per-mark CPA thresholds: mode base (400/100 m) clamped by mark
    # spacing (2026-07-10 — cross-talk fix: a threshold wider than the
    # gap between two marks let one visit emit both passes).
    thresholds = clamp_thresholds_to_spacing(
        thresholds_for_course(len(detector_marks), mode=mode),
        detector_marks,
    )
    det = GateAwareDetector(
        detector_marks,
        gates=build_gates(marks, line_bearing),
        threshold_m=thresholds,
        next_mark_index=next_idx,
        state=detector_state,
    )
    new_pass_objs = det.feed_batch(new_points)
    # Capture the AFTER-batch traversal state for the next call.
    # If a pass emitted, dump_state returns None because _reset_traversal_state
    # was called inside feed() — that's the correct "fresh" sentinel.
    new_state = det.dump_state()

    if not new_pass_objs:
        await _persist_quiet_state(
            conn,
            race_id=race_id,
            new_detector_state=new_state,
            backfill_start_at=start_at if needs_started_at_write else None,
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
    # when they need it. mark_passes + detector_state are always written
    # when we got here (new_pass_objs is non-empty by this point).
    set_parts: list[str] = [
        "mark_passes = $1::jsonb",
        "detector_state = $2::jsonb",
        "updated_at = NOW()",
    ]
    # Pass plain Python objects to the ::jsonb params — the global asyncpg
    # codec (app/db.py) json-encodes them exactly once. Do NOT json.dumps
    # here or the value gets double-encoded into a JSON string of an array
    # (the 2026-06-08 serialisation bug). None → SQL NULL.
    args: list = [
        all_passes,
        new_state,
    ]
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


async def _persist_quiet_state(
    conn: asyncpg.Connection,
    *,
    race_id: UUID,
    new_detector_state: Optional[dict],
    backfill_start_at: Optional[datetime],
) -> None:
    """Single UPDATE for the no-new-pass case.

    Writes ``detector_state`` (the load-bearing one for the cross-batch
    fix), ``started_at`` if a backfill is pending, and ``updated_at``.

    Runs unconditionally — every batch needs to persist its traversal
    state so the next batch can resume. The pre-0020 code path
    short-circuited this case with no UPDATE; with cross-batch detection
    we cannot, or the depart-confirm count is lost the moment a tiny
    batch arrives without a fresh CPA.
    """
    set_parts: list[str] = [
        "detector_state = $1::jsonb",
        "updated_at = NOW()",
    ]
    # Plain object to the ::jsonb param — the codec encodes once. None →
    # SQL NULL. (No json.dumps — see detect_and_persist_new_passes.)
    args: list = [
        new_detector_state,
    ]
    if backfill_start_at is not None:
        args.append(backfill_start_at)
        set_parts.append(f"started_at = ${len(args)}")
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
