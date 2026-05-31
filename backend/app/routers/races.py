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


class ManualMarkPassIn(BaseModel):
    """Body for ``POST /api/races/{race_id}/mark-passes`` — record one
    or more passes manually from the in-race UI.

    The UI shows every mark with a "Pass" button so the user can
    confirm any mark they've rounded that the auto-detector missed.
    Tapping mark N implies "I'm at mark N now," so the backend also
    backfills any unpassed marks before N with the same timestamp (the
    boat must have passed them too to be at N now).

    ``passed_at`` defaults to server-now when omitted. ``lat/lon`` are
    optional — the mobile UI sends them when GPS is available so the
    pass record carries the boat's actual position; without them we
    fall back to the mark's nominal position so downstream consumers
    always have a coordinate.
    """
    mark_index: int = Field(ge=0)
    passed_at: Optional[datetime] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


class MarkPassOut(BaseModel):
    """A persisted mark-rounding event. Matches the shape stored in
    ``race_sessions.mark_passes`` JSONB and the one returned by the
    track-ingest endpoints. ``source`` was added with manual passes —
    older rows lack it and the frontend treats absent as "auto".
    """
    mark_index: int
    ts: datetime
    lat: float
    lon: float
    source: Optional[Literal["auto", "manual"]] = None


class MarkPassesOut(BaseModel):
    """Response body for the manual pass endpoint — full updated list
    plus the subset that this call added (so the client can highlight
    them without diffing)."""
    mark_passes: list[MarkPassOut] = Field(default_factory=list)
    new_mark_passes: list[MarkPassOut] = Field(default_factory=list)
    ended_at: Optional[datetime] = None


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


def _marks_json(marks: list[Mark] | list[dict]) -> str:
    """Serialize marks to a JSON string for the ::jsonb cast.

    `exclude_none=True` keeps the JSONB compact when description is unset."""
    return json.dumps(
        [m.model_dump(exclude_none=True) if isinstance(m, Mark) else m for m in marks]
    )


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
            _marks_json(payload.marks),
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
            args.append(_marks_json(value))
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
            f"SELECT 1 FROM race_sessions r WHERE r.id = $1 AND {pred}",
            race_id, user["uid"],
        )
        if allowed is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
        await conn.execute(
            "DELETE FROM race_sessions WHERE id = $1", race_id,
        )
    return None


@router.post(
    "/{race_id}/mark-passes",
    response_model=MarkPassesOut,
    status_code=status.HTTP_200_OK,
)
async def record_manual_mark_pass(
    race_id: UUID,
    payload: ManualMarkPassIn,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Record a manual mark pass — fallback for when the auto-detector
    missed (or the user wants to confirm a pass live without waiting
    for the depart-confirm samples to roll in).

    UX contract (mobile in-race view):
      * Every mark is shown with a "Pass" button.
      * Tapping mark N implies "I'm at mark N now," so the server
        backfills every unpassed mark in (last_existing..N] with the
        same ``passed_at`` timestamp and ``source="manual"``.
      * Idempotent on N <= existing_max — taps on already-passed marks
        are a no-op (the response just echoes current state).
      * When the backfill includes the final mark, ``ended_at`` is set
        (COALESCEd so a prior end isn't overwritten) and the
        race-postprocess job is fired in the background.

    Authorisation uses ``race_write_predicate`` — owners and crew can
    record manual passes; viewers cannot. Returns 404 (not 403) when
    the caller can't write, matching the rest of the race routes.
    """
    # Imports here keep them out of the module load path for callers
    # that don't exercise the manual-pass endpoint (test isolation).
    from app.services.job_trigger import trigger_race_postprocess

    pred = race_write_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT r.marks, r.mark_passes
            FROM race_sessions r
            WHERE r.id = $1 AND {pred}
            """,
            race_id, user["uid"],
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")

        marks_list = _decode_marks(row["marks"])
        if not marks_list:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "race has no marks; nothing to pass",
            )
        if payload.mark_index >= len(marks_list):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"mark_index {payload.mark_index} out of range "
                f"(course has {len(marks_list)} marks)",
            )

        existing_raw = row["mark_passes"]
        if isinstance(existing_raw, (bytes, str)):
            existing = json.loads(existing_raw) if existing_raw else []
        else:
            existing = list(existing_raw or [])

        # Highest pass index already recorded. Backfill starts AT
        # next_idx (one past the highest) so we don't duplicate.
        existing_indices = {p.get("mark_index") for p in existing}
        next_idx = (
            max(existing_indices) + 1
            if existing_indices and None not in existing_indices
            else 0
        )

        if payload.mark_index < next_idx:
            # Idempotent: target already passed. Echo state, no UPDATE.
            return MarkPassesOut(
                mark_passes=[MarkPassOut(**p) for p in existing],
                new_mark_passes=[],
                ended_at=None,
            )

        passed_at = payload.passed_at or datetime.now(tz=_utc())
        # Iso-format for JSONB storage to match the existing wire shape
        # written by track_ingest.
        passed_at_iso = passed_at.isoformat()

        new_passes: list[dict] = []
        for idx in range(next_idx, payload.mark_index + 1):
            mark = marks_list[idx]
            # Use the user's GPS coordinates only on the explicitly-
            # tapped mark; backfilled marks use the nominal mark
            # position (we don't know where the boat actually was).
            if idx == payload.mark_index and payload.lat is not None and payload.lon is not None:
                lat, lon = float(payload.lat), float(payload.lon)
            else:
                lat = float(mark.get("lat", 0.0))
                lon = float(mark.get("lon", 0.0))
            new_passes.append({
                "mark_index": idx,
                "ts": passed_at_iso,
                "lat": lat,
                "lon": lon,
                "source": "manual",
            })

        all_passes = existing + new_passes
        course_complete = len(all_passes) >= len(marks_list)
        ended_at_value: Optional[datetime] = None

        set_parts = ["mark_passes = $1::jsonb", "updated_at = NOW()"]
        args: list = [json.dumps(all_passes)]
        if course_complete:
            ended_at_value = passed_at
            args.append(passed_at)
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

    # Fire post-process outside the connection block — same pattern as
    # the auto-detect path in track_ingest. Always swallowed; we never
    # let job trigger failures rollback the manual pass write.
    if course_complete:
        try:
            await trigger_race_postprocess(race_id)
        except Exception:  # noqa: BLE001
            log.warning(
                "manual mark-pass: trigger_race_postprocess failed for %s",
                race_id,
            )

    return MarkPassesOut(
        mark_passes=[MarkPassOut(**p) for p in all_passes],
        new_mark_passes=[MarkPassOut(**p) for p in new_passes],
        ended_at=ended_at_value,
    )


def _utc():
    """Lazy import of timezone — keeps the module load path lean."""
    from datetime import timezone
    return timezone.utc
