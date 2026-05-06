# backend/app/routers/routing_notifications.py
"""SSE endpoint for the 'better route available' notification stream.

GET /api/routing/notifications/{race_id}
    Server-Sent Events stream. Tails the Redis pub/sub channel
    `route:notifications:{race_id}` populated by the route_recompute
    worker. On connection, immediately replays the most recent stored
    alternative (if any) so a reconnecting client doesn't miss state.

Auth: scoped to the race owner via get_current_user. Note that the
browser EventSource API does NOT send custom headers, so the frontend
must use a fetch-based polyfill (e.g. @microsoft/fetch-event-source)
to attach the Authorization: Bearer token. Plain `new EventSource(url)`
will fail auth.

Lifecycle:
    1. Verify race ownership against race_sessions.
    2. Open Redis pubsub, subscribe to channel.
    3. Replay the current alternative (if any) as the first event.
    4. Stream new pub/sub messages as they arrive.
    5. Cleanup pubsub on client disconnect (sse-starlette cancels
       the generator; finally block runs).
"""
from __future__ import annotations

import logging
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from sse_starlette.sse import EventSourceResponse

from app import db, redis_client
from app.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/routing", tags=["routing"])


async def _event_publisher(race_id: UUID):
    """Generator that yields SSE events for one race's notification channel.

    Yields dicts in the shape sse-starlette expects:
        {"event": "alternative", "data": "<json blob>"}

    Frontend listens with addEventListener('alternative', handler). Using
    a named event (rather than the default 'message') means we can later
    add 'cycle_updated', 'route_invalidated', etc. without breaking
    existing handlers.
    """
    redis = redis_client.get_client()
    channel = f"route:notifications:{race_id}"
    alt_key = f"route:alternative:{race_id}"

    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    log.info("sse subscribed race=%s channel=%s", race_id, channel)

    try:
        # Replay the most recent stored alternative on connect. A user
        # reopening the tab or a reconnecting EventSource would otherwise
        # have to wait for the next pub/sub message - by replaying the
        # last-known state we make reconnection seamless.
        alt_blob = await redis.get(alt_key)
        if alt_blob is not None:
            data = alt_blob.decode() if isinstance(alt_blob, bytes) else alt_blob
            yield {"event": "alternative", "data": data}

        # Tail the channel. pubsub.listen() yields messages indefinitely;
        # sse-starlette's ping (default 15s) keeps the connection alive
        # through proxies and detects client disconnects by cancelling
        # this generator. The finally block then cleans up pubsub state.
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            data = message["data"]
            if isinstance(data, bytes):
                data = data.decode()
            yield {"event": "alternative", "data": data}
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception as exc:  # noqa: BLE001
            log.warning("sse cleanup failed race=%s: %s", race_id, exc)
        log.info("sse closed race=%s", race_id)


@router.get("/notifications/{race_id}")
async def notifications(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """SSE stream of better-route notifications for a single race.

    Returns 404 if the race doesn't belong to the calling user. The
    browser EventSource API auto-reconnects on transient drops; the
    initial replay inside _event_publisher ensures no state is lost
    across reconnects.

    Response is a long-lived text/event-stream. Cloud Run's default
    request timeout (60 min) caps individual connection lifetime;
    EventSource's auto-reconnect handles that transparently from the
    frontend's perspective.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM race_sessions WHERE id = $1 AND user_id = $2",
            race_id, user["uid"],
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")

    return EventSourceResponse(_event_publisher(race_id))
