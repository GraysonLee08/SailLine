"""Race session endpoints.

All routes require a valid Firebase ID token. Race planning is a Free-tier
feature (per PRD §3) — no `require_pro` gating here. In-race routing
features added later will be the gated ones.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from app.auth import get_current_user
from app.db import get_pool
from app.models.race import BoatClass, Course, RaceMode, RaceSessionCreate

router = APIRouter(prefix="/races", tags=["races"])

# How many races to return from the list endpoint. Pagination is a future
# concern — until users have hundreds of races, a fixed cap is fine.
LIST_LIMIT = 50


class RaceSession(BaseModel):
    """Stored race row, returned by all GET / POST endpoints."""
    model_config = ConfigDict(extra="forbid")

    id: UUID
    user_id: str
    name: str
    mode: RaceMode
    boat_class: BoatClass
    course: Course
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime


def _row_to_race(row: asyncpg.Record) -> RaceSession:
    """Map a DB row to the response model.

    Pydantic does the rest of the validation — including re-validating the
    course JSONB blob, which is cheap insurance against rows that pre-date a
    schema tweak.
    """
    return RaceSession.model_validate(dict(row))


# ---------------------------------------------------------------------------
# Endpoints


@router.post("", response_model=RaceSession, status_code=status.HTTP_201_CREATED)
async def create_race(
    payload: RaceSessionCreate,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> RaceSession:
    """Create a new race session for the current user."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO race_sessions (user_id, name, mode, boat_class, course)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, user_id, name, mode, boat_class, course,
                      started_at, ended_at, created_at
            """,
            user["uid"],
            payload.name,
            payload.mode.value,
            payload.boat_class.value,
            payload.course.model_dump(mode="json"),
        )
    return _row_to_race(row)


@router.get("", response_model=list[RaceSession])
async def list_races(
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[RaceSession]:
    """List the current user's races, newest first. Capped at LIST_LIMIT."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, name, mode, boat_class, course,
                   started_at, ended_at, created_at
            FROM race_sessions
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user["uid"],
            LIST_LIMIT,
        )
    return [_row_to_race(r) for r in rows]


@router.get("/{race_id}", response_model=RaceSession)
async def get_race(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> RaceSession:
    """Fetch one race by id. Returns 404 if it doesn't exist OR isn't yours.

    Treating not-yours the same as not-found prevents leaking the existence
    of other users' race ids.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, name, mode, boat_class, course,
                   started_at, ended_at, created_at
            FROM race_sessions
            WHERE id = $1 AND user_id = $2
            """,
            race_id,
            user["uid"],
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    return _row_to_race(row)


@router.delete("/{race_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_race(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> Response:
    """Delete one race by id. Same 404 semantics as GET /{race_id}."""
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            """
            DELETE FROM race_sessions
            WHERE id = $1 AND user_id = $2
            RETURNING id
            """,
            race_id,
            user["uid"],
        )
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
