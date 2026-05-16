# backend/app/routers/telemetry.py
"""Race telemetry ingestion — GPS + IMU + calibration in one batch.

POST /api/races/{race_id}/telemetry

Accepts a single batch of GPS samples (1Hz typical), IMU samples
(10-20Hz typical), and an optional calibration snapshot. Each stream
is timestamped client-side; the server persists *raw* values and
applies heel/pitch zero-offsets at query/replay time using the
``race_calibrations`` history.

Coexists with the legacy GPS-only ``/track`` endpoint. Mark-rounding
side effects (detect, persist new passes, trigger the post-process
Cloud Run Job at final mark) are identical between the two endpoints
via the shared ``app.services.track_ingest`` helper — so a client
switching from ``/track`` to ``/telemetry`` sees no behavioural drift
on auto-stop or post-race stats triggering.

Auth: scoped via ``race_write_predicate`` so the boat's crew can
record telemetry on shared boats (matches the D3 sharing model).
Cross-user / viewer access returns 404 (not 403, to avoid leaking
race existence).

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
from app.services.mark_rounding import Point as DetectorPoint
from app.services.track_ingest import (
    detect_and_persist_new_passes,
    load_race_for_ingest,
    maybe_trigger_postprocess,
)

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


class MarkPassOut(BaseModel):
    """Server-recorded rounding event.

    Mirrors the shape returned by ``/track`` so the frontend's
    auto-stop hook works against either endpoint without translation.
    """
    mark_index: int
    ts: datetime
    lat: float
    lon: float


class TelemetryAck(BaseModel):
    """Server's response — counts of rows actually inserted, plus the
    mark-rounding state after this batch.

    The client uses these to confirm its offline queue can drop
    flushed records. A successful 200 with ``gps_inserted=0,
    imu_inserted=0, calibration_inserted=False, new_mark_passes=[]``
    means the batch was accepted but empty (e.g. a heartbeat); the
    client should still drop it from the queue.

    ``mark_passes`` is the cumulative list after this batch.
    ``new_mark_passes`` is just the ones that landed in this batch —
    the auto-stop hook keys on this so it doesn't re-fire when an
    offline-queued batch flushes after the race is already complete.
    """

    gps_inserted: int
    imu_inserted: int
    calibration_inserted: bool
    mark_passes: list[MarkPassOut] = Field(default_factory=list)
    new_mark_passes: list[MarkPassOut] = Field(default_factory=list)


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

    All writes happen inside a single transaction so either the whole
    batch lands or none of it does. The client's offline queue then
    has a clean drop-on-200, retry-on-non-200 contract.

    Mark rounding runs only over the GPS portion (IMU samples don't
    have lat/lon). If the batch crosses the final mark the
    ``race-postprocess`` Cloud Run Job is kicked off after the
    transaction commits, matching the ``/track`` semantics.
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

    all_passes: list[dict] = []
    new_passes: list[dict] = []
    marks: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.transaction():
            race = await load_race_for_ingest(conn, race_id, user["uid"])
            marks = race["marks"]

            gps_inserted = 0
            if batch.gps:
                # Same unnest-based bulk insert as /track so the two
                # endpoints have parity on hot-path performance. The
                # column is ``position`` (migration 0002) — NOT
                # ``location``. PostGIS ST_MakePoint takes (lon, lat),
                # the opposite of most APIs.
                gps_ts = [s.t for s in batch.gps]
                gps_lat = [s.lat for s in batch.gps]
                gps_lon = [s.lon for s in batch.gps]
                gps_sog = [s.sog_kts for s in batch.gps]
                gps_cog = [s.cog_deg for s in batch.gps]
                gps_acc = [s.gps_acc_m for s in batch.gps]
                await conn.execute(
                    """
                    INSERT INTO track_points
                        (session_id, recorded_at, position,
                         speed_kts, heading_deg, gps_acc_m)
                    SELECT
                        $1::uuid,
                        t.recorded_at,
                        ST_SetSRID(ST_MakePoint(t.lon, t.lat), 4326)::geography,
                        t.speed_kts,
                        t.heading_deg,
                        t.gps_acc_m
                    FROM unnest(
                        $2::timestamptz[],
                        $3::float8[],
                        $4::float8[],
                        $5::float8[],
                        $6::float8[],
                        $7::float8[]
                    ) AS t(recorded_at, lat, lon,
                           speed_kts, heading_deg, gps_acc_m)
                    """,
                    race_id, gps_ts, gps_lat, gps_lon,
                    gps_sog, gps_cog, gps_acc,
                )
                gps_inserted = len(batch.gps)

                # Mark rounding runs against the GPS portion of the
                # batch only. Build detector points from the same
                # values we just persisted.
                detector_points = (
                    DetectorPoint(lat=s.lat, lon=s.lon, ts=s.t)
                    for s in batch.gps
                )
                all_passes, new_passes = await detect_and_persist_new_passes(
                    conn,
                    race_id=race_id,
                    marks=marks,
                    existing_passes=race["mark_passes"],
                    new_points=detector_points,
                )
            else:
                # No GPS in this flush — preserve the existing pass list
                # so the ack still echoes the current state.
                all_passes = list(race["mark_passes"])

            imu_inserted = 0
            if batch.imu:
                # IMU stays on executemany — schema and rates are
                # different enough that mirroring the unnest pattern
                # isn't worth the diff. Bounded at 1000 rows per batch
                # so this is fine.
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

    # Final-mark trigger lives outside the conn block (and outside the
    # transaction) so a job failure can't roll back pass persistence.
    # The trigger itself is fully tolerant of every failure mode.
    await maybe_trigger_postprocess(race_id, marks, all_passes, new_passes)

    log.info(
        "telemetry race=%s gps=%d imu=%d cal=%s new_passes=%d",
        race_id, gps_inserted, imu_inserted, calibration_inserted,
        len(new_passes),
    )
    return TelemetryAck(
        gps_inserted=gps_inserted,
        imu_inserted=imu_inserted,
        calibration_inserted=calibration_inserted,
        mark_passes=[MarkPassOut(**p) for p in all_passes],
        new_mark_passes=[MarkPassOut(**p) for p in new_passes],
    )
