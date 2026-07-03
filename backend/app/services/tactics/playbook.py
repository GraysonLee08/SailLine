"""Pre-race playbook matching — inject past-race directives into the
live tactician when today's conditions match a previous race.

Post-race analysis (prompt v4) stores a condition signature + playbook
directives on ``race_sessions.ai_summary``. This module scores today's
forecast signature against the user's past playbooks
(``race_analysis.signature.match_score``) and caches the result in
Redis so the per-batch tactician hot path never re-queries the DB.

Cache value (``tactics:playbook:{race_id}``, JSON)::

    {
      "matched": true,
      "score": 0.78,
      "signature_text": "TWS 8-12 kt, TWD ~220° with oscillating ±12°",
      "source_race_name": "Beer Can 6.18",
      "directives": ["...", ...],
    }

``matched: false`` results are cached too — a no-match race must not
re-run the query on every 30 s telemetry batch.

Failure posture matches the rest of the tactician: any error degrades
to "no playbook" and is logged, never raised to the pipeline.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

log = logging.getLogger(__name__)

# Cache TTL — outlives any race; recomputed per race_id anyway.
PLAYBOOK_TTL_S = 12 * 3600

# How much forecast to summarise for today's signature. The pipeline
# loads 1 h of forecast; sample what's there.
_SIGNATURE_SPAN_H = 1.0
_SIGNATURE_INTERVAL_S = 300

# Cap DB scan — the most recent N analysed races.
_MAX_PAST_RACES = 25

# Cap directives injected into the snapshot (token budget).
MAX_DIRECTIVES = 7


async def get_playbook(
    *,
    redis,
    pool,
    race_id: UUID,
    uid: str,
    forecast,
    lat: float,
    lon: float,
    now: datetime,
) -> Optional[dict]:
    """Cached match if present, else compute + cache. None on no match
    or any failure."""
    from app.services.redis_keys import tactics_playbook_key

    key = tactics_playbook_key(race_id)
    try:
        cached = await redis.get(key)
    except Exception as e:  # noqa: BLE001
        log.warning("playbook: redis get failed (%s)", e)
        cached = None
    if cached:
        try:
            obj = json.loads(cached.decode() if isinstance(cached, bytes) else cached)
            return obj if obj.get("matched") else None
        except (ValueError, AttributeError):
            pass  # unreadable cache — recompute below

    result = await _compute_match(
        pool=pool, race_id=race_id, uid=uid,
        forecast=forecast, lat=lat, lon=lon, now=now,
    )
    try:
        await redis.setex(key, PLAYBOOK_TTL_S, json.dumps(result).encode())
    except Exception as e:  # noqa: BLE001
        log.warning("playbook: redis setex failed (%s)", e)
    return result if result.get("matched") else None


async def _compute_match(
    *,
    pool,
    race_id: UUID,
    uid: str,
    forecast,
    lat: float,
    lon: float,
    now: datetime,
) -> dict:
    """Score today's forecast signature against past playbooks."""
    from app.services.race_analysis.signature import best_match
    from app.services.race_analysis.wind_timeline import (
        detect_events,
        forecast_rows,
        summarize_conditions,
    )

    no_match = {"matched": False}
    try:
        rows = forecast_rows(
            forecast,
            lat=lat, lon=lon,
            t_start=now,
            t_end=now + timedelta(hours=_SIGNATURE_SPAN_H),
            interval_s=_SIGNATURE_INTERVAL_S,
        )
        today = summarize_conditions(rows, detect_events(rows))
    except Exception as e:  # noqa: BLE001
        log.warning("playbook: forecast signature failed (%s)", e)
        return no_match
    if not today:
        return no_match

    try:
        async with pool.acquire() as conn:
            past = await conn.fetch(
                """
                SELECT id, name,
                       ai_summary->'playbook' AS playbook
                FROM race_sessions
                WHERE user_id = $1
                  AND id <> $2
                  AND ended_at IS NOT NULL
                  AND ai_summary ? 'playbook'
                ORDER BY ended_at DESC
                LIMIT $3
                """,
                uid, race_id, _MAX_PAST_RACES,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("playbook: past-race query failed (%s)", e)
        return no_match

    candidates: list[dict] = []
    for r in past:
        pb = r["playbook"]
        if isinstance(pb, str):
            try:
                pb = json.loads(pb)
            except ValueError:
                continue
        if not isinstance(pb, dict):
            continue
        sig = pb.get("signature")
        directives = [d for d in pb.get("directives") or [] if isinstance(d, str)]
        if not sig or not directives:
            continue
        candidates.append({
            "signature": sig,
            "signature_text": pb.get("signature_text"),
            "directives": directives,
            "race_name": r["name"],
        })
    if not candidates:
        return no_match

    winner = best_match(today, candidates)
    if winner is None:
        return no_match

    log.info(
        "playbook: matched race=%s against %r (score %.2f)",
        race_id, winner.get("race_name"), winner["score"],
    )
    return {
        "matched": True,
        "score": winner["score"],
        "signature_text": winner.get("signature_text"),
        "source_race_name": winner.get("race_name"),
        "directives": winner["directives"][:MAX_DIRECTIVES],
    }


__all__ = ["get_playbook", "PLAYBOOK_TTL_S", "MAX_DIRECTIVES"]
