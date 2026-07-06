"""FCM push delivery — thin wrapper over firebase_admin.messaging.

Used by the dead-recorder watchdog (``workers/recorder_watchdog.py``)
and any future server-initiated notification. Design constraints:

  * **Reuse the existing Firebase Admin app.** ``app/auth.py`` already
    initializes firebase-admin with ADC for token verification; FCM
    sends ride the same app (same GCP project as the client's
    google-services.json). Callers must run ``auth.initialize()``
    first — the API lifespan already does; workers call it themselves.

  * **firebase_admin.messaging.send() is blocking** (plain HTTP under
    the hood). The async orchestrator wraps each send in
    ``asyncio.to_thread`` so a slow FCM round-trip can't stall a
    worker's event loop.

  * **Self-pruning tokens.** FCM answers ``UnregisteredError`` for
    tokens whose app was uninstalled or whose registration rotated.
    ``send_to_user`` deletes those rows inline so the table converges
    on live devices without a separate cleanup job.

  * Failures never raise out of ``send_to_user`` — push is
    best-effort by nature and callers (the watchdog) must not abort a
    sweep because one device was unreachable. The return value says
    how many sends actually succeeded so callers can log/decide.
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg
from firebase_admin import messaging

log = logging.getLogger(__name__)


def _send_one(token: str, title: str, body: str, data: dict[str, str]) -> None:
    """Blocking single-token send. Raises firebase_admin exceptions.

    Notification + data payload: the notification block makes Android
    display it with zero client code even when the app is killed
    (exactly the dead-recorder scenario); the data block lets the app
    route the tap when it comes back to life.
    """
    messaging.send(
        messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=body),
            data=data,
            android=messaging.AndroidConfig(
                # High priority: FCM may wake a Doze-idle device. This
                # notification is the last line of defense for a dying
                # race recording — latency matters more than battery.
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="race-start",  # existing HIGH channel (scheduledAutoStart.ts)
                ),
            ),
        )
    )


async def send_to_user(
    pool: asyncpg.Pool,
    user_id: str,
    *,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> int:
    """Send a push to every registered device of ``user_id``.

    Returns the number of successful sends (0 = user unreachable —
    no tokens, or all sends failed). Never raises.
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT token FROM device_push_tokens WHERE user_id = $1",
                user_id,
            )
    except Exception as e:  # noqa: BLE001 — push must not break callers
        log.warning("push: token lookup failed for user %s: %s", user_id, e)
        return 0

    if not rows:
        log.info("push: user %s has no registered devices", user_id)
        return 0

    delivered = 0
    dead_tokens: list[str] = []
    for row in rows:
        token = row["token"]
        try:
            await asyncio.to_thread(_send_one, token, title, body, data or {})
            delivered += 1
        except messaging.UnregisteredError:
            # Device uninstalled the app or the token rotated — prune.
            dead_tokens.append(token)
        except Exception as e:  # noqa: BLE001 — one bad device ≠ abort
            log.warning("push: send to %s… failed: %s", token[:12], e)

    if dead_tokens:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM device_push_tokens WHERE token = ANY($1)",
                    dead_tokens,
                )
            log.info("push: pruned %d unregistered token(s)", len(dead_tokens))
        except Exception as e:  # noqa: BLE001
            log.warning("push: token prune failed: %s", e)

    return delivered
