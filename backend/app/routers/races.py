"""Race plan endpoints — pre-race CRUD.

All routes require a valid Firebase ID token via `get_current_user` and are
scoped to the calling user (no race is ever returned across users).

Marks are stored as JSONB. asyncpg returns JSONB as a string by default
(no codec registered), so we explicitly json.loads on the way out and
json.dumps on the way in. Each mark is `{name, lat, lon, description?}`.
The optional `description` lets the editor surface race book metadata for
named marks (e.g. "205° - 1.3 miles from Four Mile Crib") in hover popups.

`start_at` is the gun time for the race, stored as TIMESTAMPTZ. Nullable —
the frontend treats null as "no start time set" rather than an error,
which lets users save a course before scheduling is finalized.

`auto_start_enabled` (added in 0007) controls whether the frontend
recorder auto-starts at `start_at - 5min`. Defaults to TRUE on the
column; PATCH respects it like any other field.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import db
from app.auth import get_current_user
from app.auth_helpers import (
    race_owner_predicate,
    race_read_predicate,
    race_write_predicate,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/races", tags=["races"])


# ─── Models ──────────────────────────────────────────────────────────────

class Mark(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    description: Optional[str] = Field(default=None, max_length=500)


class RaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mode: Literal["inshore", "distance"]
    boat_class: str = Field(min_length=1, max_length=80)
    marks: list[Mark] = Field(default_factory=list)
    start_at: Optional[datetime] = None
    # Default mirrors the column default so a POST that omits the field
    # still ends up with auto_start_enabled=True. Sending False explicitly
    # at create time is supported (rare — users typically opt out later).
    auto_start_enabled: bool = True
    # D2: per-race boat link + spinnaker choice.
    boat_id: Optional[UUID] = None
    uses_spinnaker: bool = True


class RaceUpdate(BaseModel):
    """Partial update — every field is optional. PATCH semantics: send
    only what you want to change. Sending `start_at: null` explicitly
    clears it (Pydantic distinguishes "field absent" from "field is
    null" via `model_dump(exclude_unset=True)` in the SQL builder)."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    mode: Optional[Literal["inshore", "distance"]] = None
    boat_class: Optional[str] = Field(default=None, min_length=1, max_length=80)
    marks: Optional[list[Mark]] = None
    start_at: Optional[datetime] = None
    auto_start_enabled: Optional[bool] = None
    boat_id: Optional[UUID] = None
    uses_spinnaker: Optional[bool] = None


class RaceOut(BaseModel):
    id: UUID
    name: str
    mode: str
    boat_class: str
    marks: list[Mark]
    start_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    auto_start_enabled: bool = True
    boat_id: Optional[UUID] = None
    uses_spinnaker: bool = True
    # D3: who created the race. Frontend uses this to decide whether
    # to render the editor as read-only (creator vs crew/viewer).
    user_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ─── Helpers ─────────────────────────────────────────────────────────────

_SELECT_COLS = """
    id, name, mode, boat_class, marks, start_at, started_at, ended_at,
    auto_start_enabled, boat_id, uses_spinnaker, user_id,
    created_at, updated_at
"""

# Same columns, aliased to the ``r`` table for queries that JOIN
# boat_crew. Pull whichever projection your query needs.
_SELECT_COLS_R = """
    r.id, r.name, r.mode, r.boat_class, r.marks, r.start_at,
    r.started_at, r.ended_at, r.auto_start_enabled, r.boat_id,
    r.uses_spinnaker, r.user_id, r.created_at, r.updated_at
"""


def _decode_marks(value: Any) -> list[dict]:
    """asyncpg returns JSONB as str without a codec. Tolerate both."""
    if value is None:
        return []
    if isinstance(value, (bytes, str)):
        return json.loads(value)
    return value


def _row_to_race(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "mode": row["mode"],
        "boat_class": row["boat_class"],
        "marks": _decode_marks(row["marks"]),
        "start_at": row["start_at"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "auto_start_enabled": row["auto_start_enabled"],
        "boat_id": row["boat_id"],
        "uses_spinnaker": row["uses_spinnaker"],
        "user_id": row.get("user_id") if hasattr(row, "get") else row["user_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _marks_payload(marks: list[Mark] | list[dict]) -> list[dict]:
    """Normalise marks to a list of plain dicts for a ::jsonb param.

    Returns the list itself (NOT a json string) — the global asyncpg
    codec (app/db.py) json-encodes it exactly once when bound to the
    ::jsonb parameter. Passing json.dumps(...) here would double-encode
    the column into a JSON string of an array (the 2026-06-08 bug).

    `exclude_none=True` keeps the JSONB compact when description is unset."""
    return [
        m.model_dump(exclude_none=True) if isinstance(m, Mark) else m
        for m in marks
    ]


# ─── Endpoints ───────────────────────────────────────────────────────────

@router.get("", response_model=list[RaceOut])
async def list_races(
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """List races the caller can see.

    Visible races: created by the caller OR on a boat the caller is a
    member of. Legacy races (boat_id NULL, created before D2) stay
    private to their creator.
    """
    pred = race_read_predicate(race_alias="r", uid_placeholder="$1")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_SELECT_COLS_R}
            FROM race_sessions r
            WHERE {pred}
            ORDER BY r.created_at DESC
            """,
            user["uid"],
        )
    return [_row_to_race(r) for r in rows]


@router.post("", response_model=RaceOut, status_code=status.HTTP_201_CREATED)
async def create_race(
    payload: RaceCreate,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO race_sessions (
                user_id, name, mode, boat_class, marks, start_at,
                auto_start_enabled, boat_id, uses_spinnaker
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
            RETURNING {_SELECT_COLS}
            """,
            user["uid"],
            payload.name,
            payload.mode,
            payload.boat_class,
            _marks_payload(payload.marks),
            payload.start_at,
            payload.auto_start_enabled,
            payload.boat_id,
            payload.uses_spinnaker,
        )
    return _row_to_race(row)


@router.get("/{race_id}", response_model=RaceOut)
async def get_race(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    pred = race_read_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_SELECT_COLS_R}
            FROM race_sessions r
            WHERE r.id = $1 AND {pred}
            """,
            race_id,
            user["uid"],
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    return _row_to_race(row)


@router.patch("/{race_id}", response_model=RaceOut)
async def update_race(
    race_id: UUID,
    payload: RaceUpdate,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Partial update. Marks, when present, replace the entire array — we
    don't try to merge by index because reorders + edits + adds happen in
    the same form submit and a full replace is simpler + correct.

    The dynamic SET clause iterates `model_dump(exclude_unset=True)` so
    fields the client didn't send aren't touched, but fields explicitly
    set to null (e.g. clearing start_at) ARE applied as null."""
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no fields to update")

    set_parts: list[str] = []
    args: list[Any] = []
    for key, value in updates.items():
        idx = len(args) + 1
        if key == "marks":
            set_parts.append(f"marks = ${idx}::jsonb")
            args.append(_marks_payload(value))
        elif key == "ended_at":
            # Manual-stop fallback PATCH (DNF / abandoned race) must NOT
            # overwrite an authoritative ``ended_at`` already written by
            # the mark-rounding detector when the boat crossed the
            # finish. The detector's value is the closest-approach
            # timestamp to the final mark — far more accurate than
            # whatever wall-clock time the client sends from stop().
            # COALESCE keeps the first non-NULL write.
            set_parts.append(f"ended_at = COALESCE(ended_at, ${idx})")
            args.append(value)
        else:
            set_parts.append(f"{key} = ${idx}")
            args.append(value)

    set_parts.append("updated_at = NOW()")
    # Append race_id as the final placeholder for the UPDATE.
    args.append(race_id)
    id_idx = len(args)   # 1-based

    # Auth pre-check: caller can write the race (owner OR crew).
    pred = race_write_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        allowed = await conn.fetchrow(
            f"SELECT 1 FROM race_sessions r WHERE r.id = $1 AND {pred}",
            race_id, user["uid"],
        )
        if allowed is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
        sql = f"""
            UPDATE race_sessions
            SET {", ".join(set_parts)}
            WHERE id = ${id_idx}
            RETURNING {_SELECT_COLS}
        """
        row = await conn.fetchrow(sql, *args)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    return _row_to_race(row)


@router.delete("/{race_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_race(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Owner-only. Crew + viewers can't delete races, even on boats
    they're members of."""
    pred = race_owner_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        allowed = await conn.fetchrow(
            f"""SELECT 1 FROM race_sessions r WHERE r.id = $1 AND {pred}""",
            race_id, user["uid"],
        )
        if allowed is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
        await conn.execute(
            "DELETE FROM race_sessions WHERE id = $1", race_id,
        )
    return None


# ─── Manual mark pass ────────────────────────────────────────────────────

class ManualMarkPass(BaseModel):
    """Request body for POST /api/races/{race_id}/mark-pass.

    The sailor confirms via a notification action button that they
    rounded a mark the auto-detector missed. ``mark_index`` is the
    0-based index into the race's ``marks`` array; the backend inserts
    a pass at the mark's coordinates with the current timestamp and
    resets the detector so subsequent marks can be detected.
    """
    mark_index: int = Field(ge=0)


@router.post("/{race_id}/mark-pass", response_model=RaceOut)
async def manual_mark_pass(
    race_id: UUID,
    payload: ManualMarkPass,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Manually record a mark pass and advance the detector.

    Called from the mobile app's "Yes, missed it" notification action.
    Reads the current race row (FOR UPDATE to serialize against
    concurrent telemetry batches), appends a pass entry at the mark's
    coordinates with ``now()`` as the timestamp, resets
    ``detector_state`` to NULL (fresh traversal for the next mark), and
    writes ``started_at`` if still NULL (idempotent — same pattern as
    track_ingest).

    If the mark index is already present in ``mark_passes`` the request
    is a no-op (200 with the current row — idempotent on retries).

    If this pass closes the course (mark_index == len(marks) - 1),
    ``ended_at`` is written and the postprocess job is triggered —
    matching the track_ingest behaviour for auto-detected passes.
    """
    from app.services.job_trigger import trigger_race_postprocess

    pred = race_write_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                SELECT r.marks, r.mark_passes, r.started_at, r.start_at,
                       r.ended_at
                FROM race_sessions r
                WHERE r.id = $1 AND {pred}
                FOR UPDATE OF r
                """,
                race_id, user["uid"],
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

            # Validate mark_index is within range
            if payload.mark_index >= len(marks):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"mark_index {payload.mark_index} out of range "
                    f"(course has {len(marks)} marks)",
                )

            # Idempotent: if this mark already has a pass, no-op
            existing_indices = {p["mark_index"] for p in passes}
            if payload.mark_index in existing_indices:
                # Return current row — no changes needed
                row2 = await conn.fetchrow(
                    f"""SELECT {_SELECT_COLS} FROM race_sessions WHERE id = $1""",
                    race_id,
                )
                return _row_to_race(row2)

            # Build the pass entry at the mark's coordinates
            mark = marks[payload.mark_index]
            new_pass = {
                "mark_index": payload.mark_index,
                "ts": datetime.utcnow().isoformat() + "Z",
                "lat": float(mark["lat"]),
                "lon": float(mark["lon"]),
            }
            passes.append(new_pass)

            # Build the UPDATE — always write mark_passes + detector_state
            set_parts = [
                "mark_passes = $2::jsonb",
                "detector_state = NULL",
                "updated_at = NOW()",
            ]
            args: list[Any] = [race_id, passes]

            # Backfill started_at if NULL and start_at is set
            started_at = row["started_at"]
            start_at = row["start_at"]
            if started_at is None and start_at is not None:
                set_parts.append("started_at = COALESCE(started_at, $3)")
                args.append(start_at)

            # If this is the final mark, write ended_at
            is_final = payload.mark_index == len(marks) - 1
            ended_at = row["ended_at"]
            if is_final and ended_at is None:
                set_parts.append(
                    "ended_at = COALESCE(ended_at, $"
                    + str(len(args) + 1)
                    + ")"
                )
                args.append(datetime.utcnow())

            sql = f"""
                UPDATE race_sessions
                SET {", ".join(set_parts)}
                WHERE id = $1
                RETURNING {_SELECT_COLS}
            """
            updated = await conn.fetchrow(sql, *args)

    # Trigger postprocess if the final mark was just passed
    if is_final and updated and updated["ended_at"] is not None:
        # Reconstruct marks + passes for the trigger check
        all_passes = passes
        await trigger_race_postprocess(race_id)

    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    return _row_to_race(updated)


# ─── Manual race end (Stop button fallback) ──────────────────────────────
#
# When the sailor taps Stop and the auto-detector never crossed the final
# mark (missed marks, DNF, abandoned race), the postprocess job that
# generates the AI summary never fires. This endpoint sets ended_at
# (idempotent via COALESCE) and triggers the postprocess job so a summary
# is generated even when the course wasn't completed. Called from the
# mobile recording screen's handleStop after recorder.stop() finishes.

@router.post("/{race_id}/end", response_model=RaceOut)
async def manual_end_race(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Mark a race as ended and trigger the postprocess job.

    Sets ``ended_at = COALESCE(ended_at, now())`` so an authoritative
    value from the detector is never overwritten, then fires the
    race-postprocess Cloud Run Job so the AI summary is generated.

    Called from the mobile app's Stop button after recorder.stop()
    completes. Also safe to call when the detector already set
    ended_at (idempotent — the COALESCE keeps the earlier value and
    the postprocess trigger checks whether the job already ran).
    """
    from app.services.job_trigger import trigger_race_postprocess

    pred = race_write_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE race_sessions
            SET ended_at = COALESCE(ended_at, NOW()),
                updated_at = NOW()
            WHERE id = $1 AND EXISTS (
                SELECT 1 FROM race_sessions r WHERE r.id = $1 AND {pred}
            )
            RETURNING {_SELECT_COLS}
            """,
            race_id, user["uid"],
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")

    # Trigger postprocess — best-effort, never raises.
    await trigger_race_postprocess(race_id)

    return _row_to_race(row)
