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

Idempotency: the endpoint IS idempotent on duplicate flushes (as of
migration 0018, 2026-06-01). Both ``track_points`` and ``imu_samples``
have a UNIQUE (session_id, recorded_at) constraint, and the INSERT
statements use ``ON CONFLICT (session_id, recorded_at) DO NOTHING``.
A re-sent batch from the durable-queue recorder lands the new rows
and silently skips any duplicates. The ack's ``gps_inserted`` /
``imu_inserted`` report the count of rows ACTUALLY inserted (via
``RETURNING 1``), not the batch size — so a client whose response
was lost in flight can re-send the same batch, see ``gps_inserted=0``
on the second 200, and treat the batch as committed without creating
duplicates. This is the foundation for the Phase 4 native-uploader
rework (see ``sailline-docs/2026-06-01_durable-upload-pipeline-plan.md``).

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

import asyncio
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

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
    """Single GPS fix from the browser's Geolocation API.

    Sentinel coercion (2026-06-01, Phase 4): the mobile native uploader
    (Transistorsoft) emits ``-1`` for ``speed`` / ``heading`` /
    ``accuracy`` when the underlying provider hasn't computed a value,
    while the JS uploader normalises those to ``null`` client-side. To
    keep one wire shape across both code paths, this model coerces
    negative values for ``sog_kts``, ``cog_deg``, and ``gps_acc_m`` to
    ``None`` before the ``ge=0`` validation runs — same outcome the JS
    uploader produces, just deferred to the server. A single sample
    with a missing speed therefore no longer 422s the whole batch.
    """

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

    @field_validator("sog_kts", "cog_deg", "gps_acc_m", mode="before")
    @classmethod
    def _coerce_negative_sentinel_to_none(cls, v):
        """Native SDK sentinel ``-1`` (or any negative) → None.

        Runs BEFORE the ``ge=0`` constraint so negatives don't 422.
        Idempotent on already-None or already-valid values.
        """
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return v  # let pydantic surface the original error
        if f < 0:
            return None
        return f


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
                #
                # ON CONFLICT (session_id, recorded_at) DO NOTHING makes
                # the endpoint idempotent — see module docstring. We use
                # ``fetch`` + ``RETURNING 1`` instead of ``execute`` so
                # we can count the rows that ACTUALLY landed, not the
                # ones the client SENT. A re-sent batch returns
                # gps_inserted=0; a fresh batch returns the full count;
                # a partially-duplicate batch returns the in-between.
                gps_ts = [s.t for s in batch.gps]
                gps_lat = [s.lat for s in batch.gps]
                gps_lon = [s.lon for s in batch.gps]
                gps_sog = [s.sog_kts for s in batch.gps]
                gps_cog = [s.cog_deg for s in batch.gps]
                gps_acc = [s.gps_acc_m for s in batch.gps]
                inserted_rows = await conn.fetch(
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
                    ON CONFLICT (session_id, recorded_at) DO NOTHING
                    RETURNING 1
                    """,
                    race_id, gps_ts, gps_lat, gps_lon,
                    gps_sog, gps_cog, gps_acc,
                )
                gps_inserted = len(inserted_rows)

                # Mark rounding runs against the GPS portion of the
                # batch only. Build detector points from the same
                # values we just persisted.
                #
                # Detector input is the FULL batch, not just the rows
                # that landed in track_points. The two semantics:
                #   * track_points dedupes on recorded_at — a re-sent
                #     point doesn't double-store.
                #   * mark detection dedupes on next-mark-index — a
                #     re-sent point past an already-rounded mark can't
                #     re-emit the same pass (detector advances past it).
                # So passing every point through the detector is safe
                # and keeps the contract simple: detection sees the
                # client's view of reality.
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
                    started_at=race["started_at"],
                    start_at=race["start_at"],
                    mode=race["mode"],
                    detector_state=race["detector_state"],
                )
            else:
                # No GPS in this flush — preserve the existing pass list
                # so the ack still echoes the current state.
                all_passes = list(race["mark_passes"])

            imu_inserted = 0
            if batch.imu:
                # IMU uses the same idempotent INSERT pattern as GPS.
                # Switched from ``executemany`` to a single ``fetch``
                # with unnest + ON CONFLICT so we get both the
                # idempotency guarantee and an accurate ``imu_inserted``
                # count without per-row round trips.
                imu_ts = [s.t for s in batch.imu]
                imu_heel = [s.heel_deg for s in batch.imu]
                imu_pitch = [s.pitch_deg for s in batch.imu]
                imu_yaw = [s.yaw_deg for s in batch.imu]
                inserted_imu_rows = await conn.fetch(
                    """
                    INSERT INTO imu_samples
                        (session_id, recorded_at,
                         heel_deg, pitch_deg, yaw_deg)
                    SELECT
                        $1::uuid,
                        t.recorded_at,
                        t.heel_deg,
                        t.pitch_deg,
                        t.yaw_deg
                    FROM unnest(
                        $2::timestamptz[],
                        $3::float8[],
                        $4::float8[],
                        $5::float8[]
                    ) AS t(recorded_at, heel_deg, pitch_deg, yaw_deg)
                    ON CONFLICT (session_id, recorded_at) DO NOTHING
                    RETURNING 1
                    """,
                    race_id, imu_ts, imu_heel, imu_pitch, imu_yaw,
                )
                imu_inserted = len(inserted_imu_rows)

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

    # In-race tactician (Pro): fire-and-forget evaluation over the data
    # that just landed. Gated cheaply here (tier + fresh GPS) so free
    # users and IMU-only heartbeats never spawn the task; everything
    # else (cooldowns, settings opt-out, detection) is gated inside the
    # pipeline, which swallows all failures — a tactician bug can never
    # affect telemetry ingestion. Lazy import keeps the heavy
    # forecast/polar dependency chain off this router's import path.
    if batch.gps and user.get("tier") in ("pro", "hardware"):
        from app.services.tactics.pipeline import evaluate_tactics_safe
        asyncio.create_task(evaluate_tactics_safe(race_id, user["uid"]))

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
