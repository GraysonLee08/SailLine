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

import json
import logging
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from sse_starlette.sse import EventSourceResponse

from app import db, redis_client
from app.auth import get_current_user
from app.services.redis_keys import (
    route_alternative_key,
    route_notifications_channel,
    tactics_latest_key,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/routing", tags=["routing"])


def _event_name_for(data: str) -> str:
    """Map a channel message to its SSE event name.

    The channel originally carried only better-route payloads (no type
    field) — those remain event 'alternative' so existing handlers keep
    working. The in-race tactician publishes payloads with
    ``"type": "tactics"`` (2026-06-11); anything else with an explicit
    type gets its own event name so future producers don't masquerade
    as alternatives.
    """
    try:
        parsed = json.loads(data)
    except (ValueError, TypeError):
        return "alternative"
    if isinstance(parsed, dict):
        t = parsed.get("type")
        if isinstance(t, str) and t:
            return t
    return "alternative"


async def _event_publisher(race_id: UUID):
    """Generator that yields SSE events for one race's notification channel.

    Yields dicts in the shape sse-starlette expects:
        {"event": "alternative" | "tactics", "data": "<json blob>"}

    Frontend listens with addEventListener('alternative', handler) and
    addEventListener('tactics', handler). Named events mean adding a
    producer never breaks existing handlers.

    Redis timeout resilience (2026-06-29): the initial replay (alt_key
    + tactics_key reads) and the pubsub subscribe are now wrapped in
    try/except. A Redis TimeoutError during replay logs a warning and
    continues with an empty replay (the client will still receive live
    events as they arrive). A timeout during subscribe is fatal for
    this connection — the generator yields nothing and exits, which
    sse-starlette handles as a clean stream end (the client reconnects
    via EventSource's auto-reconnect). This prevents the "Truncated
    response body" crashes that killed every SSE connection during the
    Silly Race.
    """
    redis = redis_client.get_client()
    channel = route_notifications_channel(race_id)
    alt_key = route_alternative_key(race_id)
    tactics_key = tactics_latest_key(race_id)

    # Subscribe first — if this fails, the connection is toast anyway.
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(channel)
        log.info("sse subscribed race=%s channel=%s", race_id, channel)
    except Exception as exc:
        log.warning("sse subscribe failed race=%s: %s — stream will end", race_id, exc)
        return

    try:
        # Replay the most recent stored state on connect. Tolerate Redis
        # timeouts here — a failed replay is better than a crashed stream.
        try:
            alt_blob = await redis.get(alt_key)
            if alt_blob is not None:
                data = alt_blob.decode() if isinstance(alt_blob, bytes) else alt_blob
                yield {"event": "alternative", "data": data}
        except Exception as exc:
            log.warning("sse replay alt failed race=%s: %s", race_id, exc)

        try:
            tactics_blob = await redis.get(tactics_key)
            if tactics_blob is not None:
                data = (
                    tactics_blob.decode()
                    if isinstance(tactics_blob, bytes) else tactics_blob
                )
                yield {"event": "tactics", "data": data}
        except Exception as exc:
            log.warning("sse replay tactics failed race=%s: %s", race_id, exc)

        # Tail the channel. NOT pubsub.listen(): the Redis client is
        # built with socket_timeout=2 (app/redis_client.py), so a bare
        # listen() raises TimeoutError after ~2 s of channel silence —
        # which killed EVERY idle stream ~3 s after connect (observed
        # live 2026-07-11: mobile reconnect-churn at one connection per
        # ~10 s all night; invisible on web because the browser client
        # auto-reconnects and replay hides the loss).
        # get_message(timeout=1.0) polls with a read deadline UNDER the
        # socket timeout, so idle reads return None instead of raising
        # and the stream lives until the client disconnects or Cloud
        # Run's request timeout (300 s) ends it. sse-starlette's ping
        # (default 15 s) keeps proxies happy and detects client
        # disconnects by cancelling this generator; the finally block
        # then cleans up pubsub state.
        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0,
                )
            except Exception as exc:  # noqa: BLE001 — degraded, not fatal
                # A genuinely broken Redis connection would error every
                # poll; end the stream and let the client reconnect
                # (the replay makes reconnects lossless).
                log.warning(
                    "sse pubsub poll failed race=%s: %s — ending stream",
                    race_id, exc,
                )
                break
            if message is None:
                continue
            if message["type"] != "message":
                continue
            data = message["data"]
            if isinstance(data, bytes):
                data = data.decode()
            yield {"event": _event_name_for(data), "data": data}
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
    # D3: any member of the race's boat (including viewer) can
    # subscribe to better-route notifications.
    from app.auth_helpers import race_read_predicate
    pred = race_read_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT r.id FROM race_sessions r WHERE r.id = $1 AND {pred}",
            race_id, user["uid"],
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")

    # sep="\n" (2026-07-11): sse-starlette's default event separator is
    # "\r\n". Browsers parse both per the SSE spec, but react-native-sse
    # splits on a single configured lineEndingCharacter ("\n" in
    # useRouteNotifications) — under "\r\n" framing every event name
    # parsed as "tactics\r"/"alternative\r" and matched NO listener,
    # silently. Mobile never received a single SSE event before this
    # (first observed: the desk-test tactician call, 04:00:19Z).
    # "\n" framing is spec-legal and parses correctly on both clients.
    return EventSourceResponse(_event_publisher(race_id), sep="\n")
