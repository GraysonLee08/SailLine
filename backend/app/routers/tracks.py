"""Track recording endpoints - GPS breadcrumb capture during a race.

POST /api/races/{race_id}/track  bulk-inserts a batch of points (the
client buffers ~30s or ~100 points and flushes; failed flushes go back
on a localStorage queue and retry on reconnect). The same call also
runs the mark-rounding detector incrementally - any new roundings
produced by this batch are persisted to race_sessions.mark_passes
and returned in the response body so the frontend's auto-stop hook
gets immediate feedback without polling.

GET  /api/races/{race_id}/track  returns the full recorded track in
chronological order - used by the post-race playback view.

Both endpoints require Firebase auth and are scoped to the calling user
via the parent race_session's user_id - we never read or write a
race we don't own.

Schema is in migration 0002 (track_points). Position is stored as
GEOGRAPHY(POINT, 4326). On insert we build the geography from lat/lon
with ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography; on read we
project back to a geometry and pull X/Y as lon/lat.

Bulk insert uses unnest on parallel arrays in a single statement -
faster than executemany for the 30s/100-point batch sizes the
recorder produces, and keeps the round trip count at 1 per flush.

Mark-rounding state lives in race_sessions.mark_passes (JSONB,
migration 0008). Each batch reads the prior list + the boat's marks,
constructs a MarkRoundingDetector resumed at the next-unrounded
index, feeds the new batch through it, and rewrites the column with
old + new passes. See app/services/mark_rounding.py for the
algorithm and the resume semantics.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import db
from app.auth import get_current_user
from app.services.job_trigger import trigger_race_postprocess
from app.services.mark_rounding import (
    Mark as DetectorMark,
    MarkRoundingDetector,
    Point as DetectorPoint,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/races", tags=["tracks"])


# Hard cap on batch size. The recorder's flush trigger is 100 points,
# so 500 leaves headroom for retried/queued flushes from a long offline
# stretch without letting a runaway client DoS the DB.
MAX_BATCH = 500


# --- Models ------------------------------------------------------------


class TrackPointIn(BaseModel):
    """One GPS sample from the recorder.

    speed_kts and heading_deg are optional - the browser geolocation
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


class MarkPassOut(BaseModel):
    """Authoritative server-recorded rounding event.

    Same shape as the JSONB stored in race_sessions.mark_passes.
    Returned in the POST response so the frontend can update its
    auto-stop state without a follow-up GET.
    """
    mark_index: int
    ts: datetime
    lat: float
    lon: float


class TrackBatchAccepted(BaseModel):
    inserted: int
    mark_passes: list[MarkPassOut] = Field(default_factory=list)
    new_mark_passes: list[MarkPassOut] = Field(default_factory=list)


# --- Helpers -----------------------------------------------------------


async def _load_race_for_ingest(
    conn: asyncpg.Connection, race_id: UUID, uid: str
) -> dict:
    """Fetch the bits of the race row needed to run mark rounding.

    404 if the race doesn't exist OR isn't owned by this user.
    """
    row = await conn.fetchrow(
        """
        SELECT marks, mark_passes
        FROM race_sessions
        WHERE id = $1 AND user_id = $2
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
    return {"marks": marks, "mark_passes": passes}


def _detect_new_passes(
    marks: list[dict],
    existing_passes: list[dict],
    new_points: list[TrackPointIn],
) -> list[dict]:
    """Run the detector resumed at the right index over only the new
    batch. Returns just the NEW passes (not appended to existing).

    Marks without lat/lon are skipped silently - defensive against
    pre-Alembic rows that may have an odd shape.
    """
    detector_marks: list[DetectorMark] = []
    for m in marks:
        try:
            detector_marks.append(
                DetectorMark(lat=float(m["lat"]), lon=float(m["lon"]))
            )
        except (KeyError, TypeError, ValueError):
            return []
    if not detector_marks:
        return []

    next_idx = len(existing_passes)
    if next_idx >= len(detector_marks):
        return []

    det = MarkRoundingDetector(detector_marks, next_mark_index=next_idx)
    points_iter = (
        DetectorPoint(lat=p.lat, lon=p.lon, ts=p.recorded_at)
        for p in new_points
    )
    new = det.feed_batch(points_iter)
    return [
        {
            "mark_index": p.mark_index,
            "ts": p.ts.isoformat(),
            "lat": p.lat,
            "lon": p.lon,
        }
        for p in new
    ]


# --- Endpoints ---------------------------------------------------------


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
    """Bulk-insert a batch of GPS points for a race."""
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
        race = await _load_race_for_ingest(conn, race_id, user["uid"])

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

        new_passes = _detect_new_passes(
            race["marks"], race["mark_passes"], payload.points
        )

        all_passes = list(race["mark_passes"]) + new_passes
        if new_passes:
            await conn.execute(
                """
                UPDATE race_sessions
                SET mark_passes = $1::jsonb,
                    updated_at = NOW()
                WHERE id = $2 AND user_id = $3
                """,
                json.dumps(all_passes),
                race_id,
                user["uid"],
            )

    # Final-mark trigger: when this batch caused mark_passes to reach
    # the full course length, kick off the postprocess job (stats + AI
    # summary + wind snapshot). The trigger itself is a thin
    # fire-and-forget HTTP POST to Cloud Run — it never raises and
    # returns as soon as the job is accepted, so awaiting it is fine
    # (no second-long blocking). The job runs out-of-band and is
    # idempotent (skips when ai_summary is already current), so
    # multiple flushes that all cross the final-mark boundary are
    # safe.
    total_marks = len(race.get("marks") or [])
    if (
        new_passes
        and total_marks > 0
        and len(all_passes) == total_marks
    ):
        log.info(
            "race %s: final mark rounded, kicking off postprocess job",
            race_id,
        )
        await trigger_race_postprocess(race_id)

    return TrackBatchAccepted(
        inserted=n,
        mark_passes=[MarkPassOut(**p) for p in all_passes],
        new_mark_passes=[MarkPassOut(**p) for p in new_passes],
    )


@router.get("/{race_id}/track", response_model=list[TrackPointOut])
async def get_track(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Return every recorded point for the race in chronological order."""
    async with pool.acquire() as conn:
        owned = await conn.fetchrow(
            "SELECT 1 FROM race_sessions WHERE id = $1 AND user_id = $2",
            race_id,
            user["uid"],
        )
        if owned is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
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
