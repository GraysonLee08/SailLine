# backend/workers/race_sweep.py
"""Stale-race sweep — end orphaned races and run their postprocess.

Belt-and-braces for the race lifecycle (added 2026-07-02 after the Beer
Can 7.1.2026 race). Two independent failure modes can orphan a race
with ``started_at`` set but ``ended_at`` NULL:

  1. The mark detector never reaches the final mark (e.g. the pre-start
     poisoning bug, GPS dropout, user abandons the course) so
     auto-finish never fires.
  2. The mobile Stop button's ``POST /api/races/{id}/end`` call fails
     (dead cell coverage at the dock, app killed mid-request). Before
     2026-07-02 that call was silent best-effort with no retry.

An orphaned race never gets its AI summary, wind snapshot, heel or
performance summaries — the whole post-race pipeline is gated on
``ended_at``. This sweep closes the loop server-side so no dropped
client call can permanently orphan a race:

  * Find races where ``started_at IS NOT NULL AND ended_at IS NULL``
    and the most recent track point (or, if no points ever landed,
    ``started_at`` itself) is older than ``--stale-hours``.
  * Set ``ended_at = max(track_points.recorded_at)`` — the boat's last
    known fix is the honest end-of-race timestamp. Races with zero
    track points are ended at ``started_at`` (nothing to postprocess,
    but the row stops being an open race).
  * Run the standard postprocess pipeline in-process for each ended
    race (imports ``process_race`` from ``race_postprocess`` — same
    code the normal trigger path runs, no drift).

Intended schedule: hourly via Cloud Scheduler → Cloud Run Job
``race-sweep``. Locally:

    python -m workers.race_sweep --dry-run
    python -m workers.race_sweep --stale-hours 3

Idempotent: a swept race has ``ended_at`` set and never matches again.
``COALESCE(ended_at, ...)`` in the UPDATE means a concurrent legitimate
finish (final mark crossing between our SELECT and UPDATE) wins.

Exit codes: 0 = success (including "nothing to sweep"), 1 = unexpected
DB failure or any per-race postprocess failure (surfaces the execution
as failed so it shows up in Cloud Run job metrics).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app import db, redis_client
from workers.race_postprocess import process_race

log = logging.getLogger("workers.race_sweep")

# A beer can runs ~1-2 h, a distance race up to ~30 h — but the signal
# here is "no telemetry for N hours", not total duration, so a small
# default is safe for both: a boat that stopped reporting 3 h ago is
# not still racing.
DEFAULT_STALE_HOURS = 3.0


async def sweep(pool, stale_hours: float, dry_run: bool) -> int:
    """Find and close stale races; postprocess each. Returns exit code."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.id,
                   r.name,
                   r.started_at,
                   last_fix.recorded_at AS last_fix_at
            FROM race_sessions r
            LEFT JOIN LATERAL (
                SELECT max(recorded_at) AS recorded_at
                FROM track_points tp
                WHERE tp.session_id = r.id
            ) last_fix ON TRUE
            WHERE r.started_at IS NOT NULL
              AND r.ended_at IS NULL
              AND COALESCE(last_fix.recorded_at, r.started_at)
                  < NOW() - make_interval(secs => $1)
            ORDER BY r.started_at
            """,
            stale_hours * 3600.0,
        )

    if not rows:
        log.info("sweep: no stale races")
        return 0

    log.info("sweep: %d stale race(s) found", len(rows))
    failures = 0
    for row in rows:
        rid = row["id"]
        end_ts = row["last_fix_at"] or row["started_at"]
        if dry_run:
            log.info(
                "sweep (dry-run): would end race %s (%r) at %s",
                rid, row["name"], end_ts,
            )
            continue

        async with pool.acquire() as conn:
            # COALESCE: if the race legitimately finished between our
            # SELECT and this UPDATE (final-mark crossing sets ended_at),
            # the real finish wins and we just postprocess.
            await conn.execute(
                """
                UPDATE race_sessions
                SET ended_at = COALESCE(ended_at, $1),
                    updated_at = NOW()
                WHERE id = $2
                """,
                end_ts,
                rid,
            )
        log.info("sweep: ended race %s (%r) at %s", rid, row["name"], end_ts)

        if row["last_fix_at"] is None:
            # Zero track points — nothing for the postprocess to chew on.
            log.info("sweep: race %s has no track points, skipping postprocess", rid)
            continue

        try:
            rc = await process_race(pool, rid, force=False)
            if rc != 0:
                failures += 1
                log.warning("sweep: postprocess for race %s exited %d", rid, rc)
        except Exception as e:  # noqa: BLE001 — one race must not abort the rest
            failures += 1
            log.exception("sweep: postprocess for race %s failed: %s", rid, e)

    return 1 if failures else 0


async def _amain(stale_hours: float, dry_run: bool) -> int:
    await db.startup()
    # Redis is needed by the postprocess wind-snapshot step; same
    # non-fatal pattern as the API and the other workers.
    await redis_client.startup()
    try:
        pool = db.get_pool()
    except Exception as e:  # noqa: BLE001
        log.error("DB pool unavailable: %s", e)
        return 1
    try:
        return await sweep(pool, stale_hours, dry_run)
    finally:
        await db.shutdown()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description=(
            "End stale races (started but never ended, telemetry gone "
            "quiet) and run the standard post-race pipeline on each."
        ),
    )
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=DEFAULT_STALE_HOURS,
        help=(
            "End a race once its newest track point (or started_at, if "
            f"no points) is older than this. Default {DEFAULT_STALE_HOURS}."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be swept without writing anything.",
    )
    args = parser.parse_args()
    if args.stale_hours <= 0:
        parser.error("--stale-hours must be positive")
    return asyncio.run(_amain(args.stale_hours, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
