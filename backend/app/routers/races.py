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
