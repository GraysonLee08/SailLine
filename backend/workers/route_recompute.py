# backend/workers/route_recompute.py
"""Background route recomputation worker — Google-Maps-style 'better route' alerts.

Triggered after each weather_ingest cycle finishes. Walks every active
race (start_at within recompute window), runs the routing pipeline against
the freshest forecast, compares total_minutes vs the previously-cached
best, and publishes a notification when the improvement clears the threshold.

Frontend opens an SSE stream on /api/routing/notifications/{race_id} that
tails the per-race Redis pub/sub channel and surfaces the popup.

Region resolution mirrors the user-facing endpoint: the marks centroid
picks both a base region (drives wind + bathymetry) and an optional
venue (drives harbour-scale ENC hazard loading). Background recomputes
must use the same hazard set as the synchronous endpoint or "better"
routes could cut through breakwalls the user-facing route avoids — that
would surface as alerts the user immediately distrusts.

Trigger options (pick one when wiring infra):
    A. Cloud Scheduler job runs this 5 min after each ingest cycle.
    B. ingest_cycle() publishes 'cycles:updated' on Redis; this worker
       subscribes and reacts. Lower latency, more moving parts.

This file implements the recompute logic. The trigger wiring lives in
infra/ — see docs/recompute-rollout.md (TODO).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import asyncpg

from app import db, redis_client
from app.regions import base_region_for_point, venue_for_point
from app.services.bathymetry import BathymetryUnavailable
from app.services.boats import spec_for_class
from app.services.polars import load_polar
from app.services.routing import (
    DEFAULT_SAFETY_FACTOR,
    compute_isochrone_route,
    make_navigable_predicate,
    route_to_geojson,
)
from app.services.weather import ForecastNotAvailable, load_forecast_for_race

log = logging.getLogger(__name__)

# Recompute races whose start is in [-2h, +24h] from now. Negative covers
# in-progress races; positive covers pre-race plans where the user has
# already seen a route and would benefit from updates as forecasts firm up.
RECOMPUTE_WINDOW_BEFORE_HOURS = 24
RECOMPUTE_WINDOW_AFTER_HOURS = 2

# Don't pop a notification for trivial improvements. 5% on a 4-hour race
# is 12 minutes — meaningful. Tune based on user feedback.
IMPROVEMENT_THRESHOLD = 0.05

NOTIFICATION_TTL_S = 7 * 24 * 3600  # last week of alerts kept for review


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
    races = []
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


def _resolve_region(marks: list[dict]) -> tuple[str, Optional[str]]:
    """Return (base_region, venue_or_None) for the marks centroid.

    Matches the synchronous endpoint's resolver so background recomputes
    use the same hazard set as user-facing computes.
    """
    lat_c = sum(m["lat"] for m in marks) / len(marks)
    lon_c = sum(m["lon"] for m in marks) / len(marks)
    base = base_region_for_point(lat_c, lon_c)
    venue = venue_for_point(lat_c, lon_c)
    return (
        base.name if base is not None else "conus",
        venue.name if venue is not None else None,
    )


async def _read_last_total_minutes(race_id: UUID) -> Optional[float]:
    """Last total_minutes we notified about (or computed) for this race."""
    redis = redis_client.get_client()
    blob = await redis.get(f"route:last_best:{race_id}")
    return float(blob) if blob is not None else None


async def _store_last_total_minutes(race_id: UUID, total_minutes: float) -> None:
    redis = redis_client.get_client()
    await redis.setex(f"route:last_best:{race_id}",
                      NOTIFICATION_TTL_S,
                      str(total_minutes).encode())


async def _publish_better_route(race: _ActiveRace, route_feature: dict,
                                old_minutes: float, new_minutes: float) -> None:
    """Push a 'better route available' message to the per-race channel.

    Frontend SSE handler reads from this channel and shows the popup.
    Also persists the alternative route under route:alternative:{race_id}
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
    await redis.setex(f"route:alternative:{race.id}", NOTIFICATION_TTL_S, payload_blob)
    await redis.publish(f"route:notifications:{race.id}", payload_blob)
    log.info("better-route notification race=%s old=%.1fmin new=%.1fmin (-%.1f%%)",
             race.id, old_minutes, new_minutes, payload["improvement_pct"])


async def _recompute_one(race: _ActiveRace, pool: asyncpg.Pool) -> None:
    region, venue = _resolve_region(race.marks)

    try:
        spec = spec_for_class(race.boat_class)
    except Exception as exc:
        log.warning("race=%s skip: unknown boat_class %s (%s)",
                    race.id, race.boat_class, exc)
        return

    try:
        forecast = await load_forecast_for_race(
            region=region, race_start=race.start_at,
        )
    except ForecastNotAvailable:
        # Just outside HRRR yet. Re-run on the next cycle.
        return
    except RuntimeError as exc:
        log.warning("race=%s forecast load failed: %s", race.id, exc)
        return

    try:
        is_navigable = make_navigable_predicate(
            region=region, draft_m=spec.draft_m,
            safety_factor=DEFAULT_SAFETY_FACTOR,
            venue=venue,
        )
    except BathymetryUnavailable:
        log.warning("race=%s skip: no bathymetry for region=%s", race.id, region)
        return

    polar = load_polar(f"app/services/polars/{spec.polar_csv}")
    start = (race.marks[0]["lat"], race.marks[0]["lon"])
    finish = (race.marks[-1]["lat"], race.marks[-1]["lon"])

    log.info(
        "recompute race=%s region=%s venue=%s polar=%s",
        race.id, region, venue, polar.name,
    )

    result = compute_isochrone_route(
        start=start, finish=finish,
        polar=polar, wind=forecast,
        is_navigable=is_navigable,
        race_start=race.start_at,
    )
    if not result.reached:
        log.info("race=%s recompute did not reach finish — not notifying", race.id)
        return

    last = await _read_last_total_minutes(race.id)
    if last is None:
        # First time we've seen this race — establish the baseline silently.
        await _store_last_total_minutes(race.id, result.total_minutes)
        return

    if last - result.total_minutes < last * IMPROVEMENT_THRESHOLD:
        # Within noise. Refresh the baseline so a slow-drifting forecast
        # doesn't accumulate beyond threshold without ever notifying.
        await _store_last_total_minutes(race.id, result.total_minutes)
        return

    feature = route_to_geojson(result, properties={
        "race_start": race.start_at.isoformat(),
        "forecast_quality": forecast.quality,
        "polar": polar.name,
        "region": region,
        "venue": venue,
    })
    await _publish_better_route(race, feature, last, result.total_minutes)
    await _store_last_total_minutes(race.id, result.total_minutes)


async def recompute_all() -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        races = await _list_active_races(conn)
    log.info("recompute pass: %d active races", len(races))
    # Sequential — keeps Redis + Postgres load gentle and CPU contention
    # off the engine's numpy paths. Parallelize if backlog grows.
    for race in races:
        try:
            await _recompute_one(race, pool)
        except Exception as exc:  # noqa: BLE001
            log.exception("race=%s recompute failed: %s", race.id, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Route recomputation worker")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(recompute_all())


if __name__ == "__main__":
    main()
