"""Track recording endpoints — GPS breadcrumb capture during a race.

`POST /api/races/{race_id}/track`  bulk-inserts a batch of points (the
client buffers ~30s or ~100 points and flushes; failed flushes go back
on a localStorage queue and retry on reconnect).

`GET  /api/races/{race_id}/track`  returns the full recorded track in
chronological order — used by the post-race playback view.

Both endpoints require Firebase auth and are scoped to the calling user
via the parent race_session's `user_id` — we never read or write a
race we don't own.

Schema is in migration 0002 (track_points). Position is stored as
GEOGRAPHY(POINT, 4326). On insert we build the geography from lat/lon
with `ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography`; on read we
project back to a geometry and pull X/Y as lon/lat.

Bulk insert uses `unnest` on parallel arrays in a single statement —
faster than `executemany` for the 30s/100-point batch sizes the
recorder produces, and keeps the round trip count at 1 per flush.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import db
from app.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/races", tags=["tracks"])


# Hard cap on batch size. The recorder's flush trigger is 100 points,
# so 500 leaves headroom for retried/queued flushes from a long offline
# stretch without letting a runaway client DoS the DB.
MAX_BATCH = 500


# ─── Models ──────────────────────────────────────────────────────────────


class TrackPointIn(BaseModel):
    """One GPS sample from the recorder.

    `speed_kts` and `heading_deg` are optional — the browser geolocation
    API populates them on most devices but not all (e.g. desktop Safari
    with a fixed IP returns no speed/heading). Server stores nulls; the
    playback view tolerates them.
    """
    recorded_at: datetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_kts: Optional[float] = None
    heading_deg: Optional[float] = Field(default=None, ge=0, lt=360)


class TrackBatchIn(BaseModel):
    points: list[TrackPointIn] = Field(min_length=1, max_length=MAX_BATCH)


class TrackPointOut(BaseModel):
    recorded_at: datetime
    lat: float
    lon: float
    speed_kts: Optional[float] = None
    heading_deg: Optional[float] = None


class TrackBatchAccepted(BaseModel):
    inserted: int


# ─── Helpers ─────────────────────────────────────────────────────────────


async def _assert_race_owned(
    conn: asyncpg.Connection, race_id: UUID, uid: str
) -> None:
    """404 if the race doesn't exist OR isn't owned by this user.

    Same "don't leak existence" pattern as the races router — a 404 means
    "you can't have this", regardless of whether it's missing or just
    someone else's. Cheap one-row SELECT; fine to call on every flush.
    """
    row = await conn.fetchrow(
        "SELECT 1 FROM race_sessions WHERE id = $1 AND user_id = $2",
        race_id,
        uid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")


# ─── Endpoints ───────────────────────────────────────────────────────────


@router.post(
    "/{race_id}/track",
    response_model=TrackBatchAccepted,
    status_code=status.HTTP_201_CREATED,
)
async def append_track(
    race_id: UUID,
    payload: TrackBatchIn,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Bulk-insert a batch of GPS points for a race.

    Idempotency: not enforced server-side. The recorder's localStorage
    queue is the durability layer — if a flush partially succeeds, the
    client may resend points and we'll get duplicates. That's acceptable
    for v1 (post-race analysis is robust to a few duplicate samples;
    cleanup is cheap with `SELECT DISTINCT ON (recorded_at)` if needed).
    Adding a unique constraint later is a one-line migration.
    """
    n = len(payload.points)
    recorded_ats: list[datetime] = []
    lats: list[float] = []
    lons: list[float] = []
    speeds: list[Optional[float]] = []
    headings: list[Optional[float]] = []
    for p in payload.points:
        recorded_ats.append(p.recorded_at)
        lats.append(p.lat)
        lons.append(p.lon)
        speeds.append(p.speed_kts)
        headings.append(p.heading_deg)

    async with pool.acquire() as conn:
        await _assert_race_owned(conn, race_id, user["uid"])
        await conn.execute(
            """
            INSERT INTO track_points
                (session_id, recorded_at, position, speed_kts, heading_deg)
            SELECT
                $1::uuid,
                t.recorded_at,
                ST_SetSRID(ST_MakePoint(t.lon, t.lat), 4326)::geography,
                t.speed_kts,
                t.heading_deg
            FROM unnest(
                $2::timestamptz[],
                $3::float8[],
                $4::float8[],
                $5::float8[],
                $6::float8[]
            ) AS t(recorded_at, lat, lon, speed_kts, heading_deg)
            """,
            race_id,
            recorded_ats,
            lats,
            lons,
            speeds,
            headings,
        )

    return TrackBatchAccepted(inserted=n)


@router.get("/{race_id}/track", response_model=list[TrackPointOut])
async def get_track(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Return every recorded point for the race in chronological order.

    No pagination yet. A 6-hour passage at 1Hz is ~21,600 points; gzipped
    JSON of `{recorded_at, lat, lon, speed_kts, heading_deg}` per row is
    well under a few MB and the playback view loads it all upfront. If
    we start ingesting telemetry at 5–10Hz that math changes; revisit
    with cursor pagination then.
    """
    async with pool.acquire() as conn:
        await _assert_race_owned(conn, race_id, user["uid"])
        rows = await conn.fetch(
            """
            SELECT
                recorded_at,
                ST_Y(position::geometry) AS lat,
                ST_X(position::geometry) AS lon,
                speed_kts,
                heading_deg
            FROM track_points
            WHERE session_id = $1
            ORDER BY recorded_at ASC
            """,
            race_id,
        )
    return [
        TrackPointOut(
            recorded_at=r["recorded_at"],
            lat=r["lat"],
            lon=r["lon"],
            speed_kts=r["speed_kts"],
            heading_deg=r["heading_deg"],
        )
        for r in rows
    ]
