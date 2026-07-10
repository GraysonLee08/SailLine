# backend/workers/route_recompute.py
"""Background route recomputation worker — fresh routes on every ingest.

Triggered after each weather_ingest cycle. Walks every recompute-eligible
race, runs the routing pipeline against the freshest forecast, and
publishes the result to the per-race notification channel. Frontend
opens an SSE stream on ``/api/routing/notifications/{race_id}`` to tail
the per-race Redis pub/sub channel.

Eligibility (v12 — keyed to the race's computed route time)
-----------------------------------------------------------
* **Pre-start** (``start_at > now``): any race whose owner has computed
  a route at least once (a ``route:last_request:{race_id}`` blob
  exists) and whose start is within the GFS horizon. No arbitrary
  +24h cutoff — a Mac race planned 4 days out refreshes on every
  ingest from the moment its skipper first computes.
* **In progress** (``start_at <= now``, ``ended_at IS NULL``): eligible
  while ``now <= start_at + expected_minutes × LIVE_ETA_MARGIN``,
  where expected_minutes is the last computed route time (the stored
  baseline) or, cold-start, the conservative course estimate. This is
  what bounds zombie races that never got an ``ended_at`` — not a
  fixed +2h window that dropped any race longer than 2 hours.

Mid-race origin (v12)
---------------------
In-progress races recompute **from the boat's live position to the
finish**: the latest ``track_points`` fix seeds the marks list, and
``race_sessions.mark_passes`` (maintained live by the ingest-side gate
detector) tells us which marks remain. No fixes uploaded → falls back
to the full course from the start line. ``race_start`` for the engine
is ``now`` so wind is sampled in the right forecast frames.

Publish semantics (v12)
-----------------------
Every successful recompute publishes, with a ``kind`` field:

* ``"update"`` — fresh route for the newest forecast. The frontend
  applies it to the map silently (no banner).
* ``"better"`` — the fresh route beats the stored baseline by
  ≥ IMPROVEMENT_THRESHOLD. Frontend shows the accept/dismiss banner
  (pre-v12 behaviour).

Baselines are phase-scoped: a mid-race remaining-course time is not
comparable to a pre-start full-course time, so the first in-progress
pass resets the baseline silently. Payloads without ``kind`` (produced
by pre-v12 workers) are treated as ``"better"`` by the frontend for
back-compat.

Faithful replay
---------------
The synchronous endpoint stores the user's last :class:`RouteRequest`
knobs under ``route:last_request:{race_id}``. This worker reads it and
overlays the canonical race fields (marks, start_at, boat_class) from
the DB. Pre-start races *require* the stored knobs (that's the "has a
computed route" signal); in-progress races fall back to library
defaults when the blob is missing (Redis flush mid-race shouldn't
silence updates while someone is actually racing).

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
from typing import Optional
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
    estimate_course_hours,
    load_last_request,
    request_with_knobs,
)
from app.services.weather import ForecastNotAvailable

log = logging.getLogger(__name__)

# Pre-start eligibility bound: past the GFS horizon the pipeline raises
# ForecastNotAvailable anyway, so listing those races is pure waste.
PRE_START_HORIZON_HOURS = 120

# SQL guard for the in-progress side: races that started more than this
# long ago and never got an ended_at are abandoned rows, not races. The
# precise bound is the per-race ETA check below; this just keeps the
# candidate list short and index-friendly.
STARTED_LOOKBACK_DAYS = 7

# In-progress races stay eligible until start_at + expected route time ×
# this margin. Covers slow passages (light air, adverse current) without
# recomputing forever for a race whose recorder died.
LIVE_ETA_MARGIN = 1.5

# Don't pop a "better" banner for trivial improvements. 5% on a 4-hour
# race is 12 minutes — meaningful. Tune based on user feedback.
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
    mark_passes: list[dict]


def _decode_jsonb(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, (bytes, str)):
        return json.loads(raw)
    return raw


async def _list_candidate_races(conn: asyncpg.Connection) -> list[_ActiveRace]:
    """Unfinished races that *might* be eligible — the cheap SQL cut.

    Final eligibility (stored knobs pre-start, ETA window in progress)
    needs Redis and per-race arithmetic, so it happens in
    :func:`_recompute_one`.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=STARTED_LOOKBACK_DAYS)
    window_end = now + timedelta(hours=PRE_START_HORIZON_HOURS)
    rows = await conn.fetch(
        """
        SELECT id, user_id, boat_class, marks, start_at, mark_passes
        FROM race_sessions
        WHERE start_at IS NOT NULL
          AND start_at BETWEEN $1 AND $2
          AND ended_at IS NULL
        """,
        window_start, window_end,
    )
    races: list[_ActiveRace] = []
    for row in rows:
        marks = _decode_jsonb(row["marks"])
        if len(marks) < 2:
            continue
        races.append(_ActiveRace(
            id=row["id"], user_id=row["user_id"],
            boat_class=row["boat_class"], marks=marks,
            start_at=row["start_at"],
            mark_passes=_decode_jsonb(row["mark_passes"]),
        ))
    return races


async def _latest_fix(
    conn: asyncpg.Connection, race_id: UUID,
) -> Optional[tuple[float, float, datetime]]:
    """Most recent GPS fix for the race, or None if nothing uploaded."""
    row = await conn.fetchrow(
        """
        SELECT ST_Y(position::geometry) AS lat,
               ST_X(position::geometry) AS lon,
               recorded_at
        FROM track_points
        WHERE session_id = $1
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        race_id,
    )
    if row is None:
        return None
    return float(row["lat"]), float(row["lon"]), row["recorded_at"]


# ---------------------------------------------------------------------------
# Baseline — phase-scoped last route time


async def _read_baseline(race_id: UUID) -> Optional[tuple[float, str]]:
    """Return ``(total_minutes, phase)`` of the stored baseline, or None.

    v12 stores JSON ``{"total_minutes": .., "phase": "pre"|"live"}``.
    Pre-v12 workers stored a bare float — parsed as phase "pre" so
    upgrades don't spuriously re-baseline every pre-start race.
    """
    redis = redis_client.get_client()
    blob = await redis.get(route_last_best_key(race_id))
    if blob is None:
        return None
    try:
        data = json.loads(blob)
        if isinstance(data, dict) and "total_minutes" in data:
            return float(data["total_minutes"]), str(data.get("phase", "pre"))
    except (ValueError, TypeError):
        pass
    try:
        return float(blob), "pre"  # legacy bare-float format
    except (TypeError, ValueError):
        return None


async def _store_baseline(race_id: UUID, total_minutes: float, phase: str) -> None:
    redis = redis_client.get_client()
    await redis.setex(
        route_last_best_key(race_id),
        NOTIFICATION_TTL_S,
        json.dumps({"total_minutes": total_minutes, "phase": phase}).encode(),
    )


# ---------------------------------------------------------------------------
# Publish


async def _publish_route(
    race: _ActiveRace,
    route_feature: dict,
    *,
    kind: str,
    phase: str,
    computed_from: str,
    old_minutes: Optional[float],
    new_minutes: float,
) -> None:
    """Push a route payload to the per-race channel and persist it.

    ``kind`` is "update" (fresh route, applied silently) or "better"
    (clears the improvement threshold, frontend shows the banner).
    Improvement fields are zero for updates so pre-v12 payload
    consumers that read them unconditionally don't crash.
    """
    redis = redis_client.get_client()
    baseline = old_minutes if old_minutes is not None else new_minutes
    payload = {
        "race_id": str(race.id),
        "kind": kind,
        "phase": phase,
        "computed_from": computed_from,
        "old_total_minutes": baseline,
        "new_total_minutes": new_minutes,
        "improvement_minutes": max(0.0, baseline - new_minutes),
        "improvement_pct": (
            (baseline - new_minutes) / baseline * 100 if baseline > 0 else 0.0
        ),
        "route": route_feature,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    payload_blob = json.dumps(payload).encode()
    await redis.setex(route_alternative_key(race.id), NOTIFICATION_TTL_S, payload_blob)
    await redis.publish(route_notifications_channel(race.id), payload_blob)
    log.info(
        "route %s race=%s phase=%s from=%s old=%.1fmin new=%.1fmin",
        kind, race.id, phase, computed_from, baseline, new_minutes,
    )


# ---------------------------------------------------------------------------
# Per-race recompute


async def _build_pre_start_request(race: _ActiveRace) -> Optional[RouteRequest]:
    """Pre-start: stored knobs are the eligibility signal. None = skip."""
    redis = redis_client.get_client()
    knobs = await load_last_request(race.id, redis=redis)
    if knobs is None:
        log.debug("race=%s pre-start, never computed — skipping", race.id)
        return None
    return request_with_knobs(
        race_id=race.id,
        marks=race.marks,
        race_start=race.start_at,
        boat_class=race.boat_class,
        knobs=knobs,
    )


async def _build_live_request(
    race: _ActiveRace,
    conn: asyncpg.Connection,
    now: datetime,
) -> Optional[tuple[RouteRequest, str]]:
    """In-progress: route from the live position to the finish.

    Returns ``(request, computed_from)`` or None when the race is
    outside its ETA window or has no marks left to round.
    """
    baseline = await _read_baseline(race.id)
    expected_minutes = (
        baseline[0] if baseline is not None
        else estimate_course_hours(race.marks) * 60.0
    )
    deadline = race.start_at + timedelta(minutes=expected_minutes * LIVE_ETA_MARGIN)
    if now > deadline:
        log.info(
            "race=%s started %s but past ETA window (%.0f min × %.1f) — skipping",
            race.id, race.start_at.isoformat(), expected_minutes, LIVE_ETA_MARGIN,
        )
        return None

    remaining = race.marks[len(race.mark_passes):]
    if not remaining:
        # Final mark rounded; ended_at just hasn't landed yet.
        log.debug("race=%s all marks passed — skipping", race.id)
        return None

    fix = await _latest_fix(conn, race.id)
    if fix is not None:
        fix_lat, fix_lon, _fix_at = fix
        marks = [{"lat": fix_lat, "lon": fix_lon}, *remaining]
        computed_from = "live_position"
    else:
        # No telemetry (web-only user, recorder not running). Full
        # course from the start line still gives an updated strategy.
        marks = race.marks
        computed_from = "start"

    # Mid-race the recorder may be live but Redis knobs flushed — fall
    # through to defaults rather than going silent during a race.
    redis = redis_client.get_client()
    knobs = await load_last_request(race.id, redis=redis)
    if knobs is None:
        log.info("race=%s no stored knobs — library defaults for live recompute", race.id)
    req = request_with_knobs(
        race_id=race.id,
        marks=marks,
        race_start=now,
        boat_class=race.boat_class,
        knobs=knobs,
    )
    return req, computed_from


async def _recompute_one(race: _ActiveRace, conn: asyncpg.Connection) -> None:
    now = datetime.now(timezone.utc)
    phase = "live" if race.start_at <= now else "pre"

    if phase == "pre":
        req = await _build_pre_start_request(race)
        computed_from = "start"
    else:
        built = await _build_live_request(race, conn, now)
        if built is None:
            return
        req, computed_from = built
    if req is None:
        return

    redis = redis_client.get_client()
    # Pre-start uses the cache so the fresh route also lands where the
    # synchronous endpoint reads it (new ingest = new key = fresh
    # compute + warm cache for the user's next fetch). Live requests
    # are keyed on `now`, so caching them would only litter Redis.
    use_cache = phase == "pre"

    try:
        outcome = await compute_route(req, redis=redis, use_cache=use_cache)
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
        # Truncated/blocked route — never publish a partial. With the
        # v12 budget fix this now indicates a genuinely blocked course.
        log.info("race=%s recompute did not reach finish — not publishing", race.id)
        return

    new_minutes = float(outcome.meta["total_minutes"])
    baseline = await _read_baseline(race.id)

    kind = "update"
    old_minutes: Optional[float] = None
    if baseline is not None:
        last_minutes, last_phase = baseline
        # Phase change resets the comparison: remaining-course minutes
        # aren't comparable to full-course minutes.
        if last_phase == phase and last_minutes - new_minutes >= last_minutes * IMPROVEMENT_THRESHOLD:
            kind = "better"
            old_minutes = last_minutes

    await _store_baseline(race.id, new_minutes, phase)
    await _publish_route(
        race, outcome.feature,
        kind=kind, phase=phase, computed_from=computed_from,
        old_minutes=old_minutes, new_minutes=new_minutes,
    )


async def recompute_all() -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        races = await _list_candidate_races(conn)
        log.info("recompute pass: %d candidate races", len(races))
        # Sequential — keeps Redis + Postgres load gentle and CPU
        # contention off the engine's numpy paths. Parallelize if
        # backlog grows.
        for race in races:
            try:
                await _recompute_one(race, conn)
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
