# backend/app/routers/telemetry_stream.py
"""WebSocket telemetry stream — real-time IMU fusion for the gauge + advisor.

Endpoint:
    WS /api/races/{race_id}/telemetry/stream
       ?token=<firebase_id_token>
       [&resume_from_t=<float>]

Client → server: raw IMU samples at ~10 Hz, JSON text frames.
Server → client: filtered attitude at ~10 Hz + heartbeats every 15s.

Coexists with the existing REST `POST /api/races/{id}/telemetry`:
    REST owns durable storage (track_points, imu_samples, race_calibrations).
    This WebSocket owns the real-time path (live gauge today, advisor next).
    No DB writes from this handler — REST handles persistence on its 1s
    batched cadence, which is the source of truth for the historical track.

Per-connection state:
    - AttitudeFilter (Kalman, 2-axis) from app.services.attitude
    - Calibration offsets read once from race_calibrations at handshake
    - Bounded outbound queue (drop-oldest on overflow)
    - Heartbeat task to keep Cloud Run from idle-closing the connection

Handshake order (all validation before .accept()):
    1. Token present in query  → close 1008 if missing
    2. Firebase verifies token → close 1008 if invalid (via verify_ws_token)
    3. Race exists + owned     → close 1008 if not (404-style "don't leak")
    4. Load calibration offsets (default 0,0 if no calibration recorded)
    5. Accept, start heartbeat + sender tasks, enter receive loop

Resume semantics:
    `resume_from_t` is parsed and logged but not currently acted on.
    Storage is REST's job, so there's no buffered data here for the
    server to replay. The parameter establishes the protocol for future
    gap-detection metrics or replay features without a URL change.

Cloud Run notes:
    Per-connection lifetime is capped at 60 min by Cloud Run, regardless
    of how alive the connection appears. The client-side reconnect
    manager (Step 3.5) is what makes long races work — this handler just
    needs to disconnect cleanly when Cloud Run pulls the plug.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from app import db
from app.auth import InvalidTokenError, verify_ws_token
from app.services.attitude import AttitudeFilter, IMUSample

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/races", tags=["telemetry"])


# Outbound queue size. Holds ~10 s of attitude messages at 10 Hz before
# the drop-oldest backpressure kicks in. Picked so a brief client stall
# (GC pause, render reflow) doesn't lose data, but a real disconnect
# doesn't bloat memory waiting for a client that's not coming back.
OUTBOUND_QUEUE_MAX = 100

# Heartbeat cadence. Far inside Cloud Run's 5-minute idle close, and
# gives the client a steady "server is alive" signal even when the boat
# is stationary and the filter isn't producing meaningful attitude
# deltas. Monkey-patched in tests; runtime value is what's set here.
HEARTBEAT_INTERVAL_S = 15.0


# ─── Inbound schema ──────────────────────────────────────────────────────


class IMUMessage(BaseModel):
    """One IMU sample on the wire, in the phone frame.

    Field ranges are sanity bounds against malformed data, not physical
    limits. The Kalman tolerates extreme values; the bounds are here so
    a runaway client can't push us into pathological numerics. NaN and
    ±inf are rejected automatically by the ge/le checks.
    """
    t: float = Field(ge=0, description="Monotonic seconds from client")
    ax: float = Field(ge=-50, le=50)
    ay: float = Field(ge=-50, le=50)
    az: float = Field(ge=-50, le=50)
    gx: float = Field(ge=-20, le=20)
    gy: float = Field(ge=-20, le=20)
    gz: float = Field(ge=-20, le=20)


# ─── DB helpers ──────────────────────────────────────────────────────────


async def _race_belongs_to_user(
    pool: asyncpg.Pool, race_id: UUID, user_id: str
) -> bool:
    """Same 404-equivalent ownership test the REST telemetry endpoint uses.

    Cross-user access returns False here, which the handler translates
    to a 1008 close — same "don't leak whether the race exists for
    someone else" semantics.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM race_sessions WHERE id = $1 AND user_id = $2",
            race_id, user_id,
        )
    return row is not None


async def _load_calibration(
    pool: asyncpg.Pool, race_id: UUID
) -> tuple[float, float]:
    """Return (heel_offset_deg, pitch_offset_deg) from the latest calibration.

    Returns (0.0, 0.0) if no calibration has been recorded yet — the
    sailor hasn't pressed "Zero" once, so we apply no correction. The
    history table semantics (one row per re-zero, latest wins) match
    how the REST path applies offsets at read time.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT heel_zero_offset_deg, pitch_zero_offset_deg
            FROM race_calibrations
            WHERE session_id = $1
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            race_id,
        )
    if row is None:
        return 0.0, 0.0
    return (
        float(row["heel_zero_offset_deg"]),
        float(row["pitch_zero_offset_deg"]),
    )


# ─── Backpressure helper ─────────────────────────────────────────────────


def _enqueue_dropping_oldest(queue: asyncio.Queue, msg: dict) -> bool:
    """Non-blocking enqueue with drop-oldest semantics on overflow.

    Returns True iff a message was dropped to make room for the new one.
    Callers can use this for a drop-counter metric without changing
    happy-path control flow.
    """
    try:
        queue.put_nowait(msg)
        return False
    except asyncio.QueueFull:
        dropped = False
        try:
            queue.get_nowait()
            dropped = True
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Shouldn't happen — we just made room. Swallow rather than
            # crash; the message is just dropped.
            pass
        return dropped


# ─── Per-connection tasks ────────────────────────────────────────────────


async def _heartbeat_loop(queue: asyncio.Queue) -> None:
    """Enqueue a heartbeat every HEARTBEAT_INTERVAL_S seconds.

    Heartbeats go through the same outbound queue as attitude messages so
    the sender task is the single point of write to the WebSocket. If
    the queue is full of attitude messages we drop the heartbeat rather
    than displacing real data — a missed heartbeat is recoverable, a
    missed attitude sample is not.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        msg = {"type": "heartbeat", "t": time.time()}
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass


async def _sender_loop(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Drain the outbound queue, write to the WebSocket.

    Runs as its own task so a slow client (full TCP receive buffer on
    their end → send_text awaits) can't stall our receive loop. The
    receive loop's drop-oldest is how we shed load if the client falls
    behind; this loop just writes whatever the queue gives it.
    """
    while True:
        msg = await queue.get()
        await websocket.send_text(json.dumps(msg))


# ─── Endpoint ────────────────────────────────────────────────────────────


@router.websocket("/{race_id}/telemetry/stream")
async def telemetry_stream(
    websocket: WebSocket,
    race_id: UUID,
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> None:
    """Real-time IMU stream with server-side Kalman fusion."""
    token = websocket.query_params.get("token")
    resume_from_t = websocket.query_params.get("resume_from_t")

    # Validation, all before .accept() so failures look like a clean
    # handshake rejection rather than an accept-then-close.
    if not token:
        await websocket.close(code=1008, reason="missing token")
        return

    try:
        user = await verify_ws_token(token, pool)
    except InvalidTokenError as exc:
        await websocket.close(code=1008, reason=f"invalid token: {exc}")
        return

    if not await _race_belongs_to_user(pool, race_id, user["uid"]):
        await websocket.close(code=1008, reason="race not found")
        return

    heel_offset, pitch_offset = await _load_calibration(pool, race_id)

    await websocket.accept()
    log.info(
        "ws telemetry open race=%s user=%s heel_off=%.2f pitch_off=%.2f resume_from_t=%s",
        race_id, user["uid"], heel_offset, pitch_offset, resume_from_t,
    )

    # Per-connection state
    filter_ = AttitudeFilter()
    outbound: asyncio.Queue = asyncio.Queue(maxsize=OUTBOUND_QUEUE_MAX)
    sample_count = 0
    drop_count = 0
    bad_message_count = 0

    heartbeat_task = asyncio.create_task(_heartbeat_loop(outbound))
    sender_task = asyncio.create_task(_sender_loop(websocket, outbound))

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                msg = IMUMessage.model_validate_json(raw)
            except ValidationError:
                bad_message_count += 1
                # Sample-log to keep noise down if a buggy client is
                # streaming pure garbage. We don't close — most bad
                # messages are transient client bugs, and closing on
                # every parse error would cause reconnect storms.
                if bad_message_count % 50 == 1:
                    log.debug(
                        "ws telemetry race=%s malformed (count=%d)",
                        race_id, bad_message_count,
                    )
                continue

            result = filter_.step(IMUSample(
                t=msg.t,
                ax=msg.ax, ay=msg.ay, az=msg.az,
                gx=msg.gx, gy=msg.gy, gz=msg.gz,
            ))
            if result is None:
                # First sample (no dt yet) or pathological timestamp gap.
                # The filter handles these by skipping — we follow suit.
                continue

            heel_deg, pitch_deg = result
            out_msg = {
                "type": "attitude",
                "t": msg.t,
                "heel_deg": heel_deg - heel_offset,
                "pitch_deg": pitch_deg - pitch_offset,
            }

            if _enqueue_dropping_oldest(outbound, out_msg):
                drop_count += 1
            sample_count += 1

    except WebSocketDisconnect:
        log.info(
            "ws telemetry close race=%s user=%s samples=%d drops=%d bad=%d",
            race_id, user["uid"], sample_count, drop_count, bad_message_count,
        )
    except Exception:
        # Anything else is a server-side bug or DB / network surprise.
        # Log with traceback and close 1011 (internal error). Don't
        # propagate — that would let Starlette log a second, less
        # informative copy.
        log.exception(
            "ws telemetry race=%s user=%s unhandled error",
            race_id, user["uid"],
        )
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    finally:
        heartbeat_task.cancel()
        sender_task.cancel()
        # Await cancellation so the tasks finish cleanly before this
        # handler returns — keeps asyncio from logging "task was
        # destroyed but it is pending" warnings.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender_task