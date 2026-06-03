# backend/workers/route_recompute.py
"""Background route recomputation worker — 'better route' alerts.

Triggered after each weather_ingest cycle. Walks every active race
(``start_at`` within the recompute window), runs the routing pipeline
against the freshest forecast, compares total_minutes vs the
previously-stored baseline, and publishes a notification when the
improvement clears the threshold. Frontend opens an SSE stream on
``/api/routing/notifications/{race_id}`` to tail the per-race Redis
pub/sub channel.

Faithful replay
---------------
The synchronous endpoint stores the user's last :class:`RouteRequest`
under ``route:last_request:{race_id}``. This worker reads it and
overlays the canonical race fields (marks, start_at, boat_class) from
the DB onto a fresh :class:`RouteRequest`. That way:

* The same ``safety_factor``, ``duration_hours``, and
  :class:`DeratingProfile` the user chose are applied here.
* A race that has been rescheduled or had marks moved since the user
  last computed gets the new canonical state, not stale snapshots.

When no stored request exists (cold start: Redis flushed, or race
never opened in the editor since this code shipped) the worker falls
back to library defaults — the same values the previous hardcoded
worker used. Cold-start fallbacks are logged so we can monitor the
frequency.

Trigger options (pick one when wiring infra):
    A. Cloud Scheduler job runs this 5 min after each ingest cycle.
    B. ingest_cycle() publishes 'cycles:updated' on Redis; this worker
       subscribes and reacts. Lower latency, more moving parts.

This file implements the recompute logic. The trigger wiring lives in
infra/ — see docs/recompute-rollout.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg

from app import db, redis_client
from app.services.bathymetry import BathymetryUnavailable
from app.services.redis_keys import (
    ROUTE_NOTIFICATION_TTL_S,
    route_alternative_key,
    route_last_best_key,
    route_notifications_channel,
)
from app.services.routing import (
    RouteRequest,
    compute_route,
    load_last_request,
    request_with_knobs,
)
from app.services.weather import ForecastNotAvailable

log = logging.getLogger(__name__)

# Recompute races whose start is in [-2h, +24h] from now. Negative covers
# in-progress races; positive covers pre-race plans where the user has
# already seen a route and would benefit from updates as forecasts firm up.
RECOMPUTE_WINDOW_BEFORE_HOURS = 24
RECOMPUTE_WINDOW_AFTER_HOURS = 2

# Don't pop a notification for trivial improvements. 5% on a 4-hour race
# is 12 minutes — meaningful. Tune based on user feedback.
IMPROVEMENT_THRESHOLD = 0.05

# Re-exported for the test suite. Kept as a module constant for now;
# moves to ``app.services.redis_keys`` if we ever want a single TTL knob.
NOTIFICATION_TTL_S = ROUTE_NOTIFICATION_TTL_S


@dataclass
class _ActiveRace:
    id: UUID
    user_id: str
    boat_class: str
    marks: list[dict]
    start_at: datetime


async def _list_active_races(conn: asyncpg.Connection) -> list[_ActiveRace]:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=RECOMPUTE_WINDOW_AFTER_HOURS)
    window_end = now + timedelta(hours=RECOMPUTE_WINDOW_BEFORE_HOURS)
    rows = await conn.fetch(
        """
        SELECT id, user_id, boat_class, marks, start_at
        FROM race_sessions
        WHERE start_at IS NOT NULL
          AND start_at BETWEEN $1 AND $2
          AND ended_at IS NULL
        """,
        window_start, window_end,
    )
    races: list[_ActiveRace] = []
    for row in rows:
        marks_raw = row["marks"]
        marks = json.loads(marks_raw) if isinstance(marks_raw, (bytes, str)) else (marks_raw or [])
        if len(marks) < 2:
            continue
        races.append(_ActiveRace(
            id=row["id"], user_id=row["user_id"],
            boat_class=row["boat_class"], marks=marks,
            start_at=row["start_at"],
        ))
    return races


async def _read_last_total_minutes(race_id: UUID) -> float | None:
    """Last total_minutes we notified about (or computed) for this race."""
    redis = redis_client.get_client()
    blob = await redis.get(route_last_best_key(race_id))
    return float(blob) if blob is not None else None


async def _store_last_total_minutes(race_id: UUID, total_minutes: float) -> None:
    redis = redis_client.get_client()
    await redis.setex(
        route_last_best_key(race_id),
        NOTIFICATION_TTL_S,
        str(total_minutes).encode(),
    )


async def _publish_better_route(
    race: _ActiveRace,
    route_feature: dict,
    old_minutes: float,
    new_minutes: float,
) -> None:
    """Push a 'better route available' message to the per-race channel.

    Frontend SSE handler reads from this channel and shows the popup.
    Also persists the alternative route under ``route:alternative:{race_id}``
    so the user can fetch it after dismissing the popup.
    """
    redis = redis_client.get_client()
    payload = {
        "race_id": str(race.id),
        "old_total_minutes": old_minutes,
        "new_total_minutes": new_minutes,
        "improvement_minutes": old_minutes - new_minutes,
        "improvement_pct": (old_minutes - new_minutes) / old_minutes * 100,
        "route": route_feature,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    payload_blob = json.dumps(payload).encode()
    await redis.setex(route_alternative_key(race.id), NOTIFICATION_TTL_S, payload_blob)
    await redis.publish(route_notifications_channel(race.id), payload_blob)
    log.info(
        "better-route notification race=%s old=%.1fmin new=%.1fmin (-%.1f%%)",
        race.id, old_minutes, new_minutes, payload["improvement_pct"],
    )


async def _build_request(race: _ActiveRace) -> RouteRequest:
    """Build the :class:`RouteRequest` to feed the pipeline.

    Reads the user's stored knobs (set by the endpoint on every
    successful compute) and overlays the canonical race fields. Cold
    start fallback uses library defaults and is logged.
    """
    redis = redis_client.get_client()
    knobs = await load_last_request(race.id, redis=redis)
    if knobs is None:
        log.info(
            "race=%s no stored knobs — using library defaults for recompute",
            race.id,
        )
    return request_with_knobs(
        race_id=race.id,
        marks=race.marks,
        race_start=race.start_at,
        boat_class=race.boat_class,
        knobs=knobs,
    )


async def _recompute_one(race: _ActiveRace) -> None:
    req = await _build_request(race)
    redis = redis_client.get_client()

    try:
        outcome = await compute_route(req, redis=redis, use_cache=False)
    except ForecastNotAvailable:
        # Just outside the model horizon. Re-run on the next cycle.
        return
    except BathymetryUnavailable as exc:
        log.warning("race=%s skip: %s", race.id, exc)
        return
    except RuntimeError as exc:
        log.warning("race=%s forecast/setup failed: %s", race.id, exc)
        return

    if not outcome.meta.get("reached"):
        log.info("race=%s recompute did not reach finish — not notifying", race.id)
        return

    new_minutes = float(outcome.meta["total_minutes"])
    last = await _read_last_total_minutes(race.id)
    if last is None:
        # First time we've seen this race — establish the baseline silently.
        await _store_last_total_minutes(race.id, new_minutes)
        return

    if last - new_minutes < last * IMPROVEMENT_THRESHOLD:
        # Within noise. Refresh the baseline so a slow-drifting forecast
        # doesn't accumulate beyond threshold without ever notifying.
        await _store_last_total_minutes(race.id, new_minutes)
        return

    await _publish_better_route(race, outcome.feature, last, new_minutes)
    await _store_last_total_minutes(race.id, new_minutes)


async def recompute_all() -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        races = await _list_active_races(conn)
    log.info("recompute pass: %d active races", len(races))
    # Sequential — keeps Redis + Postgres load gentle and CPU contention
    # off the engine's numpy paths. Parallelize if backlog grows.
    for race in races:
        try:
            await _recompute_one(race)
        except Exception as exc:  # noqa: BLE001
            log.exception("race=%s recompute failed: %s", race.id, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Route recomputation worker")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(recompute_all())


if __name__ == "__main__":
    main()
