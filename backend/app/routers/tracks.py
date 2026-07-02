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

Both endpoints require Firebase auth and are scoped via
``race_write_predicate`` (POST) and ``race_read_predicate`` (GET) so
crew members on a shared boat can record and view, but viewers can
only view.

Schema is in migration 0002 (track_points). Position is stored as
GEOGRAPHY(POINT, 4326). On insert we build the geography from lat/lon
with ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography; on read we
project back to a geometry and pull X/Y as lon/lat.

Bulk insert uses unnest on parallel arrays in a single statement -
faster than executemany for the 30s/100-point batch sizes the
recorder produces, and keeps the round trip count at 1 per flush.

Idempotency (added 2026-06-01, migration 0018): the INSERT uses
``ON CONFLICT (session_id, recorded_at) DO NOTHING`` so a re-sent
batch from the recorder's offline queue lands the new rows and
silently skips duplicates. ``inserted`` in the response reports the
ACTUAL landed count via ``RETURNING 1``, not the batch size — so a
client whose response was lost in flight can re-send and tell the
batch was committed without creating duplicate rows.

The whole batch (INSERT + race row read + mark-pass UPDATE) now runs
inside ``conn.transaction()`` so the ``FOR UPDATE`` row lock that
``load_race_for_ingest`` acquires holds for the read-modify-write of
``mark_passes``. Two concurrent batches against the same race
serialize cleanly instead of stomping each other.

Mark-rounding side effects (detect, persist new passes, trigger
post-process job at the final mark) are delegated to
``app.services.track_ingest`` so the same behaviour applies whether
the batch comes in via this router or via the newer ``/telemetry``
endpoint. The detector algorithm itself lives in
``app/services/mark_rounding.py``.
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
from app.auth_helpers import race_read_predicate
from app.services.mark_rounding import Point as DetectorPoint
from app.services.track_ingest import (
    detect_and_persist_new_passes,
    load_race_for_ingest,
    maybe_trigger_postprocess,
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

    inserted_count = 0
    async with pool.acquire() as conn:
        # Wrap the load + INSERT + detect-and-update in a single
        # transaction so the FOR UPDATE row lock acquired inside
        # load_race_for_ingest holds across the whole read-modify-write
        # on race_sessions.mark_passes. Without the transaction the
        # SELECT FOR UPDATE releases its lock at statement boundary and
        # two concurrent batches can race on the JSONB update.
        async with conn.transaction():
            race = await load_race_for_ingest(conn, race_id, user["uid"])

            inserted_rows = await conn.fetch(
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
                ON CONFLICT (session_id, recorded_at) DO NOTHING
                RETURNING 1
                """,
                race_id,
                recorded_ats,
                lats,
                lons,
                speeds,
                headings,
            )
            inserted_count = len(inserted_rows)

            # Detector input is the FULL batch (see /telemetry note).
            # Mark detection self-dedupes on next-mark-index, so a
            # re-sent point past an already-rounded mark can't re-emit
            # a pass — safe to feed every point through.
            detector_points = (
                DetectorPoint(lat=p.lat, lon=p.lon, ts=p.recorded_at)
                for p in payload.points
            )
            all_passes, new_passes = await detect_and_persist_new_passes(
                conn,
                race_id=race_id,
                marks=race["marks"],
                existing_passes=race["mark_passes"],
                new_points=detector_points,
                started_at=race["started_at"],
                start_at=race["start_at"],
                mode=race["mode"],
                detector_state=race["detector_state"],
                start_line_bearing_override=race["start_line_bearing_override"],
                start_line_bearing_deg=race["start_line_bearing_deg"],
            )

    # Final-mark trigger lives outside the conn block so a job failure
    # can't rollback the pass persistence. The trigger itself is fully
    # tolerant of every failure mode (missing env var, no ADC, network
    # error) so awaiting it is safe even in dev.
    await maybe_trigger_postprocess(
        race_id, race["marks"], all_passes, new_passes,
    )

    # inserted reports the rows that ACTUALLY landed in track_points,
    # not the rows the client sent. A re-sent batch returns
    # ``inserted=0`` even though all the points were already stored —
    # the client treats that as "batch committed, drop from queue."
    return TrackBatchAccepted(
        inserted=inserted_count,
        mark_passes=[MarkPassOut(**p) for p in all_passes],
        new_mark_passes=[MarkPassOut(**p) for p in new_passes],
    )


@router.get("/{race_id}/track", response_model=list[TrackPointOut])
async def get_track(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Return every recorded point for the race in chronological order.

    Read access: caller created the race OR is a member of the boat
    at any role (including viewer)."""
    pred = race_read_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        owned = await conn.fetchrow(
            f"SELECT 1 FROM race_sessions r WHERE r.id = $1 AND {pred}",
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
