# backend/app/routers/telemetry.py
"""Race telemetry ingestion — GPS + IMU + calibration in one batch.

POST /api/races/{race_id}/telemetry

Accepts a single batch of GPS samples (1Hz typical), IMU samples
(10-20Hz typical), and an optional calibration snapshot. Each stream
is timestamped client-side; the server persists *raw* values and
applies heel/pitch zero-offsets at query/replay time using the
``race_calibrations`` history.

Coexists with the existing GPS-only ``/track`` endpoint. New client
code (heel/pitch capture, AbsoluteOrientationSensor wiring) targets
``/telemetry``; ``/track`` stays for the deployed track-recording
flow until it can be migrated.

Auth: scoped to the race owner via ``get_current_user``. A user can
only post telemetry to their own races; cross-user access returns 404
(not 403, to avoid leaking race existence).

Idempotency: the endpoint is *not* idempotent on duplicate flushes —
a re-sent batch from the offline queue inserts duplicate rows. We
accept this in v1 because deduping in Postgres on a high-rate hot
path is more expensive than the storage cost of the duplicates, and
read-side queries can dedupe on (session_id, recorded_at). v2: a
unique index + ``ON CONFLICT DO NOTHING`` if duplicates become
material.

Sign conventions (apply consistently across client + server + UI):

* ``heel_deg``  positive = starboard rail down
* ``pitch_deg`` positive = bow up
* ``yaw_deg``   degrees true (0 = north, 90 = east), from IMU
                magnetometer. Used to cross-check GPS COG when SOG
                is below the GPS-velocity threshold.
* ``cog_deg``   degrees true.

The dev plan calls out heel/pitch/*roll*; we ship heel/pitch/*yaw*
because heel and roll refer to the same axis (redundant), while yaw
is genuinely useful for the at-rest / low-speed heading cross-check.
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

router = APIRouter(prefix="/api/races", tags=["telemetry"])


# ─── Batch limits ────────────────────────────────────────────────────────
#
# Sized so legitimate offline-burst flushes succeed but pathological
# clients don't blow up the DB. Online flush cadence is ~1s, so a
# 100-sample GPS cap = ~100s of buffered offline data, and a
# 1000-sample IMU cap = ~50s at 20Hz. If the offline queue grows past
# this, the client should split into multiple batches.

MAX_GPS_SAMPLES_PER_BATCH = 100
MAX_IMU_SAMPLES_PER_BATCH = 1000


# ─── Models ──────────────────────────────────────────────────────────────


class GpsSample(BaseModel):
    """Single GPS fix from the browser's Geolocation API."""

    t: datetime = Field(
        description="Sample timestamp, ISO 8601 with millisecond precision."
    )
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    sog_kts: Optional[float] = Field(
        default=None, ge=0, le=60,
        description="Speed over ground, knots. Null if the GPS hasn't "
                    "computed velocity yet (typical for the first 1-2 fixes).",
    )
    cog_deg: Optional[float] = Field(
        default=None, ge=0, lt=360,
        description="Course over ground, degrees true. Null when SOG "
                    "is below the device's GPS velocity threshold.",
    )
    gps_acc_m: Optional[float] = Field(
        default=None, ge=0,
        description="95% horizontal accuracy radius, meters. Filter "
                    "on this in queries to reject low-quality fixes.",
    )


class ImuSample(BaseModel):
    """Single IMU reading from AbsoluteOrientationSensor (or fallback chain).

    Raw values, unmodified by client-side calibration. The server
    applies heel/pitch zero-offsets at read time using
    ``race_calibrations``.
    """

    t: datetime = Field(
        description="Sample timestamp, ISO 8601 with millisecond precision."
    )
    heel_deg: float = Field(
        ge=-90, le=90,
        description="Roll about the boat's longitudinal axis. "
                    "Positive = starboard rail down.",
    )
    pitch_deg: float = Field(
        ge=-90, le=90,
        description="Pitch about the boat's lateral axis. "
                    "Positive = bow up.",
    )
    yaw_deg: float = Field(
        ge=0, lt=360,
        description="Yaw, degrees true. From the IMU magnetometer. "
                    "Cross-checks GPS COG when SOG is too low for "
                    "the GPS to compute course reliably.",
    )


class Calibration(BaseModel):
    """Heel/pitch zero-offsets captured when the boat is at rest, level.

    Sent in the same batch as the next telemetry flush after the user
    presses 'Zero' on the calibration UI. A new row is appended; older
    rows are not modified. Apply at read time as
    ``heel_corrected = heel_deg - heel_zero_offset_deg`` for samples
    where ``sample.t >= calibration.captured_at`` and no later
    calibration has superseded.
    """

    captured_at: datetime
    heel_zero_offset_deg: float = Field(ge=-90, le=90)
    pitch_zero_offset_deg: float = Field(ge=-90, le=90)


class TelemetryBatch(BaseModel):
    """One flush from the client.

    All three fields are optional. Real-world flushes will look like:

    * GPS-only — IMU permission denied, or sensor unavailable
    * IMU-only — between GPS fixes (rare; flush cadence usually
      includes both)
    * GPS + IMU — the common case
    * GPS + IMU + calibration — first flush after the user re-zeroes
    """

    gps: list[GpsSample] = Field(default_factory=list)
    imu: list[ImuSample] = Field(default_factory=list)
    calibration: Optional[Calibration] = None


class TelemetryAck(BaseModel):
    """Server's response — counts of rows actually inserted.

    The client uses these to confirm its offline queue can drop
    flushed records. A successful 200 with ``gps_inserted=0,
    imu_inserted=0, calibration_inserted=False`` means the batch was
    accepted but empty (e.g. a heartbeat); the client should still
    drop it from the queue.
    """

    gps_inserted: int
    imu_inserted: int
    calibration_inserted: bool


# ─── Helpers ─────────────────────────────────────────────────────────────


async def _verify_race_ownership(
    conn: asyncpg.Connection, race_id: UUID, user_id: str
) -> None:
    """404 if the race doesn't exist OR isn't owned by the caller.

    We deliberately return 404 (not 403) for cross-user access to
    avoid leaking the existence of other users' races.
    """
    row = await conn.fetchrow(
        "SELECT 1 FROM race_sessions WHERE id = $1 AND user_id = $2",
        race_id, user_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="race not found",
        )


# ─── Endpoint ────────────────────────────────────────────────────────────


@router.post(
    "/{race_id}/telemetry",
    response_model=TelemetryAck,
    status_code=status.HTTP_200_OK,
)
async def post_telemetry(
    race_id: UUID,
    batch: TelemetryBatch,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> TelemetryAck:
    """Persist a batch of GPS + IMU + calibration to the race session.

    All three writes happen inside a single transaction so either the
    whole batch lands or none of it does. The client's offline queue
    then has a clean drop-on-200, retry-on-non-200 contract.
    """
    if len(batch.gps) > MAX_GPS_SAMPLES_PER_BATCH:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"gps batch exceeds {MAX_GPS_SAMPLES_PER_BATCH} samples",
        )
    if len(batch.imu) > MAX_IMU_SAMPLES_PER_BATCH:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"imu batch exceeds {MAX_IMU_SAMPLES_PER_BATCH} samples",
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _verify_race_ownership(conn, race_id, user["uid"])

            gps_inserted = 0
            if batch.gps:
                # PostGIS ST_MakePoint takes (lon, lat) — the opposite
                # of most APIs. Cast to geography so distance/proximity
                # queries against the route geometry work without
                # reprojection.
                gps_rows = [
                    (race_id, s.t, s.lon, s.lat, s.sog_kts,
                     s.cog_deg, s.gps_acc_m)
                    for s in batch.gps
                ]
                await conn.executemany(
                    """
                    INSERT INTO track_points
                        (session_id, recorded_at, location,
                         speed_kts, heading_deg, gps_acc_m)
                    VALUES
                        ($1, $2, ST_MakePoint($3, $4)::geography,
                         $5, $6, $7)
                    """,
                    gps_rows,
                )
                gps_inserted = len(gps_rows)

            imu_inserted = 0
            if batch.imu:
                imu_rows = [
                    (race_id, s.t, s.heel_deg, s.pitch_deg, s.yaw_deg)
                    for s in batch.imu
                ]
                await conn.executemany(
                    """
                    INSERT INTO imu_samples
                        (session_id, recorded_at,
                         heel_deg, pitch_deg, yaw_deg)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    imu_rows,
                )
                imu_inserted = len(imu_rows)

            calibration_inserted = False
            if batch.calibration is not None:
                await conn.execute(
                    """
                    INSERT INTO race_calibrations
                        (session_id, captured_at,
                         heel_zero_offset_deg, pitch_zero_offset_deg)
                    VALUES ($1, $2, $3, $4)
                    """,
                    race_id,
                    batch.calibration.captured_at,
                    batch.calibration.heel_zero_offset_deg,
                    batch.calibration.pitch_zero_offset_deg,
                )
                calibration_inserted = True

    log.info(
        "telemetry race=%s gps=%d imu=%d cal=%s",
        race_id, gps_inserted, imu_inserted, calibration_inserted,
    )
    return TelemetryAck(
        gps_inserted=gps_inserted,
        imu_inserted=imu_inserted,
        calibration_inserted=calibration_inserted,
    )
