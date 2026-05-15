# backend/workers/race_postprocess.py
"""Post-race postprocess — run once per race after the boat finishes.

Triggered by ``app/routers/tracks.py`` the moment the final mark is
detected as rounded. Runs as a Cloud Run Job in production; locally
it can be invoked directly:

    python -m workers.race_postprocess --race-id <uuid>
    python -m workers.race_postprocess --race-id <uuid> --force

What it does, in order:

  1. Load the race row + its track points + marks + mark_passes.
  2. Compute stats (``services/race_stats.compute_stats``).
  3. Snapshot the live wind forecast over the race window, IF the
     forecast is still in Redis and the marks resolve to a known
     region.
  4. Generate an AI recap+tips via Anthropic (``services/race_summary``).
  5. UPDATE race_sessions: ``ai_summary``, ``wind_snapshot``.

Why a separate job, not in-line in the POST track endpoint:

  * The Anthropic call is 1-5 seconds. Blocking the user's track flush
    on it is a bad bargain.
  * Decoupling lets us regenerate independently (``--force``) without
    touching the recorder code path.
  * Failures here (rate limit, transient Anthropic error, missing
    forecast cycles) don't affect the user-facing track ingest. The
    stats endpoint surfaces "summary unavailable" until the job is
    rerun.

Idempotency:

  * If ``ai_summary`` already exists with the current PROMPT_VERSION,
    we skip the Anthropic call (saves cost). The wind snapshot is
    refreshed only if it's missing — otherwise we keep what we have,
    because the forecast we used at race-end is the right one to keep.
  * ``--force`` overrides both checks.

The job exits 0 on success OR on graceful skip (e.g. "no track yet").
It exits 1 only when an unexpected DB error or assertion failure
makes the state inconsistent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import asyncpg

from app import db, redis_client
from app.regions import base_region_for_point
from app.services.race_stats import (
    compute_stats,
    track_points_from_rows,
)
from app.services.race_summary import (
    PROMPT_VERSION,
    generate_summary,
)
from app.services.weather.forecast_loader import (
    ForecastNotAvailable,
    load_forecast_for_race,
)
from app.services.wind_snapshot import marks_bbox, snapshot_forecast

log = logging.getLogger("workers.race_postprocess")


# Hard cap on race duration for forecast loading. Mac-length races run
# ~30 h; 36 h gives margin for slow finishers without pulling more
# GFS fhours than we need.
_MAX_FORECAST_DURATION_H = 36.0

# Pad either side of the actual track window so the snapshot covers
# the pre-start and a bit of cooldown after the finish.
_SNAPSHOT_PAD_H = 0.25  # 15 min


# ─── DB I/O ───────────────────────────────────────────────────────────


async def _load_race(
    pool: asyncpg.Pool, race_id: UUID,
) -> Optional[dict]:
    """Pull every column we need to run the pipeline in a single query.

    Returns None if the race doesn't exist.

    D2: LEFT JOIN the boats table so corrected-time math has the rating
    available without a second round trip.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                r.id, r.user_id, r.name, r.boat_class, r.start_at,
                r.marks, r.mark_passes, r.ai_summary, r.wind_snapshot,
                r.mode, r.uses_spinnaker, r.boat_id,
                b.hcp    AS boat_hcp,
                b.dhcp   AS boat_dhcp,
                b.nshcp  AS boat_nshcp,
                b.dnshcp AS boat_dnshcp
            FROM race_sessions r
            LEFT JOIN boats b ON b.id = r.boat_id
            WHERE r.id = $1
            """,
            race_id,
        )
        if row is None:
            return None
        return dict(row)


async def _load_track(
    pool: asyncpg.Pool, race_id: UUID,
) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                recorded_at,
                ST_Y(position::geometry) AS lat,
                ST_X(position::geometry) AS lon,
                speed_kts,
                heading_deg
            FROM track_points
            WHERE session_id = $1
            ORDER BY recorded_at ASC
            """,
            race_id,
        )
    return [dict(r) for r in rows]


async def _persist(
    pool: asyncpg.Pool,
    race_id: UUID,
    *,
    ai_summary: Optional[dict],
    wind_snapshot: Optional[dict],
) -> None:
    """UPDATE race_sessions with whatever new fields we produced.

    Both fields are JSONB. Pass None to leave a column untouched —
    we only overwrite a column when we have a fresh value for it.
    """
    sets: list[str] = []
    args: list = [race_id]
    i = 2
    if ai_summary is not None:
        sets.append(f"ai_summary = ${i}::jsonb")
        args.append(json.dumps(ai_summary))
        i += 1
    if wind_snapshot is not None:
        sets.append(f"wind_snapshot = ${i}::jsonb")
        args.append(json.dumps(wind_snapshot))
        i += 1
    if not sets:
        return
    sets.append("updated_at = NOW()")
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE race_sessions SET {', '.join(sets)} WHERE id = $1",
            *args,
        )


# ─── Wind snapshot orchestration ──────────────────────────────────────


async def _build_wind_snapshot(
    marks: list[dict],
    track_started: datetime,
    track_ended: datetime,
) -> Optional[dict]:
    """Resolve the region, load the live forecast, freeze it.

    Returns None when:
      * No marks, or marks centroid falls outside every base region.
      * The forecast isn't in Redis (cycle expired, never ingested).
      * The forecast doesn't intersect the race window.

    Returning None is not an error — the AI summary just won't have
    wind context for this race.
    """
    bbox = marks_bbox(marks)
    if bbox is None:
        log.info("wind snapshot: no usable marks; skipping")
        return None

    lat_c = sum(float(m["lat"]) for m in marks) / len(marks)
    lon_c = sum(float(m["lon"]) for m in marks) / len(marks)
    base = base_region_for_point(lat_c, lon_c)
    if base is None:
        log.info(
            "wind snapshot: marks centroid (%.3f, %.3f) outside all base regions; "
            "skipping",
            lat_c, lon_c,
        )
        return None

    # Pad the track window slightly so the snapshot covers pre-start
    # positioning and a few minutes after the finish.
    pad = _SNAPSHOT_PAD_H * 3600
    t_start = track_started - timedelta(seconds=pad)
    t_end = track_ended + timedelta(seconds=pad)
    duration_h = min(
        _MAX_FORECAST_DURATION_H,
        (t_end - t_start).total_seconds() / 3600.0,
    )

    try:
        forecast = await load_forecast_for_race(
            region=base.name,
            race_start=t_start,
            duration_hours=duration_h,
        )
    except ForecastNotAvailable as e:
        log.info("wind snapshot: forecast not yet available (%s); skipping", e)
        return None
    except RuntimeError as e:
        # No ingested cycles or no intersecting snapshots — race likely
        # finished long ago and Redis dropped the data.
        log.info("wind snapshot: forecast load failed (%s); skipping", e)
        return None

    return snapshot_forecast(
        forecast,
        bbox=bbox,
        t_start=t_start,
        t_end=t_end,
    )


# ─── Orchestrator ─────────────────────────────────────────────────────


def _summary_is_current(existing: Optional[dict]) -> bool:
    """True iff the existing summary was produced by the current
    prompt version and looks well-formed."""
    if not existing:
        return False
    return (
        existing.get("prompt_version") == PROMPT_VERSION
        and isinstance(existing.get("recap"), str)
    )


async def process_race(
    pool: asyncpg.Pool,
    race_id: UUID,
    *,
    force: bool = False,
) -> int:
    """Run the pipeline for one race. Returns a process exit code:
    0 on success or graceful skip, 1 on a state-inconsistent error.

    Designed so the Cloud Run Job exit code reflects "should this be
    retried" — graceful skips return 0 so Cloud Tasks doesn't
    redeliver, while DB-level errors return 1 to trigger retry.
    """
    race = await _load_race(pool, race_id)
    if race is None:
        log.warning("race %s not found; nothing to do", race_id)
        return 0

    track_rows = await _load_track(pool, race_id)
    if not track_rows:
        log.info("race %s has no track points; nothing to summarise", race_id)
        return 0

    track_points = track_points_from_rows(track_rows)
    marks = race["marks"] or []
    mark_passes = race["mark_passes"] or []
    race_start_at = race["start_at"]

    boat_for_math = None
    if race.get("boat_id"):
        boat_for_math = {
            "hcp": race.get("boat_hcp"),
            "dhcp": race.get("boat_dhcp"),
            "nshcp": race.get("boat_nshcp"),
            "dnshcp": race.get("boat_dnshcp"),
        }
    stats = compute_stats(
        track_points,
        marks=marks,
        mark_passes=mark_passes,
        race_start_at=race_start_at,
        boat=boat_for_math,
        mode=race.get("mode"),
        uses_spinnaker=bool(race.get("uses_spinnaker", True)),
    )
    if stats is None:
        log.info("race %s: stats compute returned None; nothing to do", race_id)
        return 0

    # Wind snapshot — refresh if missing or forced. Otherwise keep the
    # one we already have (it captured the conditions at race-end,
    # which is the right thing to preserve).
    new_snapshot: Optional[dict] = None
    if force or not race.get("wind_snapshot"):
        new_snapshot = await _build_wind_snapshot(
            marks=marks,
            track_started=stats.started_at,
            track_ended=stats.ended_at,
        )

    # AI summary — skip the call if a current version already exists.
    new_summary: Optional[dict] = None
    if force or not _summary_is_current(race.get("ai_summary")):
        # Use the freshly-built snapshot if we have one, else the
        # already-stored one (so the summary still gets wind context).
        snapshot_for_prompt = new_snapshot or race.get("wind_snapshot")
        new_summary = generate_summary(
            race_name=race.get("name"),
            boat_class=race.get("boat_class"),
            stats=stats.to_dict(),
            wind_snapshot=snapshot_for_prompt,
        )
        if new_summary is None:
            log.warning(
                "race %s: AI summary generation returned None "
                "(missing key, API error, or unparseable response)",
                race_id,
            )

    await _persist(
        pool, race_id,
        ai_summary=new_summary,
        wind_snapshot=new_snapshot,
    )
    log.info(
        "race %s: postprocess complete (summary=%s, snapshot=%s)",
        race_id,
        "regenerated" if new_summary else "skipped",
        "refreshed" if new_snapshot else "skipped",
    )
    return 0


# ─── Entrypoint ───────────────────────────────────────────────────────


async def _amain(race_id: UUID, force: bool) -> int:
    await db.startup()
    # Redis is needed by the forecast loader. Same non-fatal pattern as
    # the API — if redis is down, the wind snapshot step gracefully
    # returns None.
    await redis_client.startup()
    try:
        pool = db.get_pool()
    except Exception as e:  # noqa: BLE001
        log.error("DB pool unavailable: %s", e)
        return 1
    try:
        return await process_race(pool, race_id, force=force)
    finally:
        await db.shutdown()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description=(
            "Post-race postprocess: compute stats, snapshot wind, "
            "generate AI recap, persist to race_sessions."
        ),
    )
    parser.add_argument(
        "--race-id",
        required=True,
        help="UUID of the race_sessions row to process.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Regenerate even when ai_summary already matches the "
            "current prompt version and wind_snapshot is present."
        ),
    )
    args = parser.parse_args()
    try:
        race_id = UUID(args.race_id)
    except ValueError:
        log.error("--race-id must be a UUID: %r", args.race_id)
        return 2
    return asyncio.run(_amain(race_id, force=args.force))


if __name__ == "__main__":
    sys.exit(main())
