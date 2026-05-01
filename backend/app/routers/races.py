"""Race plan endpoints — pre-race CRUD.

All routes require a valid Firebase ID token via `get_current_user` and are
scoped to the calling user (no race is ever returned across users).

Marks are stored as JSONB. asyncpg returns JSONB as a string by default
(no codec registered), so we explicitly json.loads on the way out and
json.dumps on the way in. Each mark is `{name, lat, lon, description?}`.
The optional `description` lets the editor surface race book metadata for
named marks (e.g. "205° - 1.3 miles from Four Mile Crib") in hover popups.
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


class RaceUpdate(BaseModel):
    """Partial update — every field is optional. PATCH semantics: send
    only what you want to change."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    mode: Optional[Literal["inshore", "distance"]] = None
    boat_class: Optional[str] = Field(default=None, min_length=1, max_length=80)
    marks: Optional[list[Mark]] = None


class RaceOut(BaseModel):
    id: UUID
    name: str
    mode: str
    boat_class: str
    marks: list[Mark]
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


# ─── Helpers ─────────────────────────────────────────────────────────────

_SELECT_COLS = """
    id, name, mode, boat_class, marks, started_at, ended_at,
    created_at, updated_at
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
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
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
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_SELECT_COLS}
            FROM race_sessions
            WHERE user_id = $1
            ORDER BY created_at DESC
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
            INSERT INTO race_sessions (user_id, name, mode, boat_class, marks)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            RETURNING {_SELECT_COLS}
            """,
            user["uid"],
            payload.name,
            payload.mode,
            payload.boat_class,
            _marks_json(payload.marks),
        )
    return _row_to_race(row)


@router.get("/{race_id}", response_model=RaceOut)
async def get_race(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_SELECT_COLS}
            FROM race_sessions
            WHERE id = $1 AND user_id = $2
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
    the same form submit and a full replace is simpler + correct."""
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
    args.extend([race_id, user["uid"]])
    id_idx = len(args) - 1
    uid_idx = len(args)

    sql = f"""
        UPDATE race_sessions
        SET {", ".join(set_parts)}
        WHERE id = ${id_idx} AND user_id = ${uid_idx}
        RETURNING {_SELECT_COLS}
    """
    async with pool.acquire() as conn:
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
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM race_sessions WHERE id = $1 AND user_id = $2",
            race_id,
            user["uid"],
        )
    if result.rsplit(" ", 1)[-1] == "0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    return None
