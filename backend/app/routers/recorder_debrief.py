# backend/app/routers/recorder_debrief.py
"""Recorder debrief — per-session diagnostic blob posted by the mobile
recorder at ``stop()``.

POST /api/races/{race_id}/recorder-debrief

Phase 2 of the durable upload pipeline rework
(``sailline-docs/2026-06-01_durable-upload-pipeline-plan.md``).

Why this exists
---------------

The 2026-05-31 on-water race recorded 2,090 GPS points but the backend
received them in three bursts separated by long silences, then a final
2h 47m gap. We learned that from Cloud Logging, which is slow,
sensitive to filter shape, and unavailable to the user themselves.

This endpoint flips the diagnostic visibility: the recorder ships its
own view of the session (counts, gap durations, recent log) the moment
it stops. Future failures show up server-side without anyone digging
through Cloud Logging.

Schema versioning
-----------------

The JSONB ``payload`` carries ``schema_version`` (currently 1). The
endpoint does NOT validate the inner shape past pydantic's tolerance —
a v2 recorder shipping new fields can post against a v1 backend and
both still work. The post-race UI and analytics layer are responsible
for handling unknown fields gracefully.

Insert-only contract
--------------------

Each stop() inserts a new row. Multiple sessions on the same race
(user stops + restarts, or auto-stop edge cases) produce multiple
debriefs ordered by ``created_at``. "Latest debrief for race X" is
the common read; the index added in migration 0019 makes it fast.

We never UPDATE a debrief. If the recorder wants to amend (rare), it
posts again — the latest row wins for the "current state" view, and
the older rows remain visible to history queries.

Auth
----

Scoped via ``race_write_predicate`` — same boat-crew model as the
telemetry endpoints. Cross-user attempts return 404 (not 403) to
avoid leaking race existence.

Failure tolerance
-----------------

The mobile recorder calls this best-effort during stop() and does NOT
block teardown on the response. So the endpoint should be cheap and
not let a debrief failure cascade. Validation errors return 422; we
do not silently accept malformed payloads (better to surface the bug
than store junk).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import db
from app.auth import get_current_user
from app.auth_helpers import race_write_predicate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/races", tags=["recorder-debrief"])


# Caps to bound DB row size. The mobile shape ships at most 50 log
# entries today; we accept a bit of headroom in case a future recorder
# version wants more. A pathological client posting a 10 MB blob would
# otherwise quietly bloat the table.
MAX_RECENT_LOG_ENTRIES = 200
MAX_LOG_MESSAGE_CHARS = 200


# ─── Models ──────────────────────────────────────────────────────────────


class RecorderLogEntry(BaseModel):
    """One entry from the recorder's on-device ring buffer.

    Matches the ``RecorderLogEntry`` type on the mobile side. All
    fields except ``ts`` and ``kind`` are optional so the recorder
    can append shape-flexible entries as the platform evolves.
    """

    ts: str = Field(description="ISO-8601 timestamp of when this entry was recorded.")
    kind: str = Field(description="One of 'flush', 'lifecycle', 'error'.")
    status: Optional[str] = Field(
        default=None,
        description="One of 'ok', 'error', 'info'. None when not applicable.",
    )
    http_status: Optional[int] = None
    duration_ms: Optional[float] = None
    batch_size: Optional[int] = None
    inserted: Optional[int] = None
    queue_depth_after: Optional[int] = None
    message: Optional[str] = Field(
        default=None,
        max_length=MAX_LOG_MESSAGE_CHARS,
        description=(
            f"Short note. Truncated to {MAX_LOG_MESSAGE_CHARS} chars to "
            "keep row size bounded."
        ),
    )


class DeviceInfo(BaseModel):
    """Device / build identifiers — useful for triaging failures back
    to a specific EAS build."""

    platform: str = Field(description="'ios' or 'android'.")
    os_version: Optional[str] = None
    app_version: Optional[str] = None
    build_id: Optional[str] = Field(
        default=None,
        description="EAS build id (e.g. 72f7aaa8-...) when available.",
    )


class SessionTiming(BaseModel):
    """Wall-clock bounds of this recording session."""

    start_ts: str
    end_ts: str
    duration_s: int


class CaptureStats(BaseModel):
    """GPS-capture counts.

    ``points_uploaded`` = sum of server-reported ``gps_inserted``
    across all successful telemetry POSTs. With the Phase 1
    idempotency contract this matches what's actually in
    ``track_points``; a re-sent batch doesn't double-count here
    because the server reports zero for the duplicate.
    """

    points_captured: int
    points_uploaded: int
    points_remaining_in_queue: int
    max_queue_depth: int


class UploadStats(BaseModel):
    """Upload-attempt statistics.

    ``longest_success_gap_s`` is the largest interval between two
    successful (200) POSTs during the session. This is the most
    important single number for spotting an upload outage — a healthy
    race has this <30 seconds; the 2026-05-31 race would show ~9,000.
    """

    attempts: int
    successes: int
    http_5xx: int
    http_4xx: int
    network_errors: int
    longest_success_gap_s: float


class RecorderDebrief(BaseModel):
    """The full debrief payload. ``schema_version`` MUST be 1 today;
    future bumps go through a backwards-compatible field-add path.
    """

    schema_version: int = Field(ge=1, le=1)
    device: DeviceInfo
    session: SessionTiming
    capture: CaptureStats
    uploads: UploadStats
    recent_log: list[RecorderLogEntry] = Field(
        default_factory=list,
        max_length=MAX_RECENT_LOG_ENTRIES,
        description=(
            f"Tail of the on-device ring buffer, up to "
            f"{MAX_RECENT_LOG_ENTRIES} entries. Older entries stay on "
            "device for the debug screen and are not shipped."
        ),
    )


class RecorderDebriefAck(BaseModel):
    """Response shape — returns the row id so the mobile recorder can
    note it locally (handy for cross-referencing the debug screen
    against the server row later)."""

    id: UUID
    created_at: str


# ─── Endpoint ────────────────────────────────────────────────────────────


@router.post(
    "/{race_id}/recorder-debrief",
    response_model=RecorderDebriefAck,
    status_code=status.HTTP_201_CREATED,
)
async def post_recorder_debrief(
    race_id: UUID,
    debrief: RecorderDebrief,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> RecorderDebriefAck:
    """Persist a recorder debrief blob.

    Two-step (auth-check then insert) — the auth check is a SELECT
    against the race row, the insert is a single INSERT against
    ``recorder_debriefs``. We do NOT wrap in a transaction: the auth
    SELECT and the INSERT are independent, the INSERT is one
    statement, and the read-after-write contract isn't needed (the
    client uses the returned id, not the visible-to-others state).
    """
    pred = race_write_predicate(race_alias="r", uid_placeholder="$2")

    async with pool.acquire() as conn:
        owned = await conn.fetchrow(
            f"""
            SELECT 1
            FROM race_sessions r
            WHERE r.id = $1 AND {pred}
            """,
            race_id,
            user["uid"],
        )
        if owned is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="race not found"
            )

        # JSONB column gets the pydantic-validated dict. asyncpg's
        # global codec (registered in app/db.py) accepts a plain Python
        # dict for JSONB params — no manual json.dumps needed.
        payload: dict[str, Any] = debrief.model_dump(mode="json")

        row = await conn.fetchrow(
            """
            INSERT INTO recorder_debriefs (session_id, payload)
            VALUES ($1, $2::jsonb)
            RETURNING id, created_at
            """,
            race_id,
            json.dumps(payload),
        )

    assert row is not None  # INSERT with RETURNING always returns a row
    log.info(
        "recorder_debrief race=%s id=%s captured=%d uploaded=%d gap_s=%.1f",
        race_id, row["id"],
        debrief.capture.points_captured,
        debrief.capture.points_uploaded,
        debrief.uploads.longest_success_gap_s,
    )
    return RecorderDebriefAck(
        id=row["id"],
        created_at=row["created_at"].isoformat(),
    )
