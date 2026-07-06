# backend/workers/recorder_watchdog.py
"""Dead-recorder watchdog — push "recording stopped" while it still matters.

The fast, notify-only sibling of ``workers/race_sweep.py``. The sweep
closes orphaned races hours after the fact; THIS worker's job is to
tell the sailor **mid-race** that their phone went quiet, while
there's still a race left to save (added 2026-07-05 after a race was
lost to a silently-dead recorder).

Predicate: a race with ``started_at`` set, ``ended_at`` NULL, and a
newest track point older than ``--silence-minutes`` (default 5) but
newer than ``--max-silence-hours`` (default 3, matching the sweep's
stale window — beyond that the sweep owns the race and a push is just
noise about a race that's over). Races whose ``started_at`` came from
a mark-pass but that never landed a single fix have no ``last_fix``
and are skipped: with zero uploads there is no baseline to measure
silence against (the T-6/T-5 auto-start notifications are the guard
for the never-started class).

Notify policy (state in Redis, key ``watchdog:recorder:{race_id}``):

  * First detection of silence → push.
  * Same silence on later ticks → quiet until ``RENOTIFY_MINUTES``
    (15) has passed, then one reminder, repeating.
  * Fixes resumed and went silent AGAIN (state's last_fix_at older
    than the current one) → re-arm and push immediately: a new
    failure deserves a new alert.
  * Redis unavailable → **skip sends entirely.** Without dedup state
    a 2-minute cadence would spam a push per tick; silence is the
    safer failure mode, and the hourly sweep still backstops the race
    itself.

Push delivery is ``app/services/push.py`` (FCM via the already-
initialized firebase-admin app). No pushes registered for the owner →
logged, nothing else to do.

Intended schedule: every 2 minutes via Cloud Scheduler → Cloud Run Job
``recorder-watchdog``. Locally:

    python -m workers.recorder_watchdog --dry-run
    python -m workers.recorder_watchdog --silence-minutes 3

Exit codes: 0 = success (including "nothing silent"), 1 = unexpected
DB failure (surfaces as a failed execution in Cloud Run job metrics).
Individual push failures do NOT fail the run — they're logged and the
next tick retries naturally.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

from app import auth, db, redis_client
from app.services.push import send_to_user
from app.services.redis_keys import (
    RECORDER_WATCHDOG_TTL_S,
    recorder_watchdog_key,
)

log = logging.getLogger("workers.recorder_watchdog")

DEFAULT_SILENCE_MINUTES = 5.0
DEFAULT_MAX_SILENCE_HOURS = 3.0
RENOTIFY_MINUTES = 15.0


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def should_notify(
    state_json: str | None,
    last_fix_at: datetime,
    now: datetime,
    renotify_minutes: float = RENOTIFY_MINUTES,
) -> bool:
    """Pure decision: given the stored notify-state, push again?

    Exposed at module level (not nested in the sweep loop) so the
    tests can pin the matrix without a DB or Redis.
    """
    if state_json is None:
        return True  # first detection of this silence
    try:
        state = json.loads(state_json)
        state_fix = datetime.fromisoformat(state["last_fix_at"])
        notified_at = datetime.fromisoformat(state["notified_at"])
    except (KeyError, TypeError, ValueError):
        return True  # unreadable state — treat as absent
    if last_fix_at > state_fix:
        return True  # fixes resumed, then went silent again — new failure
    return (now - notified_at).total_seconds() >= renotify_minutes * 60


async def sweep(
    pool,
    redis,
    silence_minutes: float,
    max_silence_hours: float,
    dry_run: bool,
) -> int:
    """Find silent recorders, push once per policy. Returns exit code."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.id,
                   r.name,
                   r.user_id,
                   last_fix.recorded_at AS last_fix_at
            FROM race_sessions r
            JOIN LATERAL (
                SELECT max(recorded_at) AS recorded_at
                FROM track_points tp
                WHERE tp.session_id = r.id
            ) last_fix ON last_fix.recorded_at IS NOT NULL
            WHERE r.started_at IS NOT NULL
              AND r.ended_at IS NULL
              AND last_fix.recorded_at < NOW() - make_interval(secs => $1)
              AND last_fix.recorded_at > NOW() - make_interval(secs => $2)
            ORDER BY last_fix.recorded_at
            """,
            silence_minutes * 60.0,
            max_silence_hours * 3600.0,
        )

    if not rows:
        log.info("watchdog: no silent recorders")
        return 0

    log.info("watchdog: %d silent recorder(s) found", len(rows))
    now = datetime.now(timezone.utc)

    for row in rows:
        rid = row["id"]
        last_fix_at: datetime = row["last_fix_at"]
        owner = row["user_id"]
        silent_min = int((now - last_fix_at).total_seconds() // 60)

        if owner is None:
            log.warning("watchdog: race %s has no owner, skipping", rid)
            continue

        if redis is None:
            # No dedup state available — do not push (see module doc).
            log.warning(
                "watchdog: redis unavailable, would have checked race %s "
                "(silent %d min) — skipping sends this tick",
                rid, silent_min,
            )
            continue

        key = recorder_watchdog_key(rid)
        state_json = await redis.get(key)
        if isinstance(state_json, bytes):
            state_json = state_json.decode()
        if not should_notify(state_json, last_fix_at, now):
            log.info(
                "watchdog: race %s still silent (%d min), already notified",
                rid, silent_min,
            )
            continue

        if dry_run:
            log.info(
                "watchdog (dry-run): would push for race %s (%r) — "
                "silent %d min, owner %s",
                rid, row["name"], silent_min, owner,
            )
            continue

        delivered = await send_to_user(
            pool,
            owner,
            title="SailLine — recording stopped",
            body=(
                f"No GPS from “{row['name']}” for {silent_min} min. "
                "Open SailLine to resume recording."
            ),
            data={"kind": "deadRecorder", "raceId": str(rid)},
        )
        log.info(
            "watchdog: race %s (%r) silent %d min — push delivered to "
            "%d device(s)",
            rid, row["name"], silent_min, delivered,
        )

        # Record the notify regardless of delivered count: zero devices
        # today will still be zero devices in 2 minutes, and re-pushing
        # into the void every tick just burns FCM quota + log noise.
        # The RENOTIFY window retries naturally.
        await redis.set(
            key,
            json.dumps(
                {"last_fix_at": _iso(last_fix_at), "notified_at": _iso(now)},
            ),
            ex=RECORDER_WATCHDOG_TTL_S,
        )

    return 0


async def _amain(
    silence_minutes: float, max_silence_hours: float, dry_run: bool,
) -> int:
    # firebase-admin app for FCM sends (same ADC identity as the API).
    auth.initialize()
    await db.startup()
    await redis_client.startup()
    try:
        pool = db.get_pool()
    except Exception as e:  # noqa: BLE001
        log.error("DB pool unavailable: %s", e)
        return 1
    try:
        redis = redis_client.get_client()
    except Exception:  # noqa: BLE001 — non-fatal, sweep() handles None
        redis = None
    try:
        return await sweep(pool, redis, silence_minutes, max_silence_hours, dry_run)
    finally:
        await db.shutdown()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description=(
            "Push a 'recording stopped' notification to the owner of any "
            "open race whose telemetry has gone silent."
        ),
    )
    parser.add_argument(
        "--silence-minutes",
        type=float,
        default=DEFAULT_SILENCE_MINUTES,
        help=(
            "Notify once the newest track point is older than this. "
            f"Default {DEFAULT_SILENCE_MINUTES}."
        ),
    )
    parser.add_argument(
        "--max-silence-hours",
        type=float,
        default=DEFAULT_MAX_SILENCE_HOURS,
        help=(
            "Ignore races silent longer than this — race_sweep owns them. "
            f"Default {DEFAULT_MAX_SILENCE_HOURS} (keep = sweep's window)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be pushed without sending or writing state.",
    )
    args = parser.parse_args()
    if args.silence_minutes <= 0:
        parser.error("--silence-minutes must be positive")
    if args.max_silence_hours * 60 <= args.silence_minutes:
        parser.error("--max-silence-hours must exceed --silence-minutes")
    return asyncio.run(
        _amain(args.silence_minutes, args.max_silence_hours, args.dry_run),
    )


if __name__ == "__main__":
    sys.exit(main())
