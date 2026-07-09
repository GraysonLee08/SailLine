"""Tactician observability endpoint (Phase A, 2026-07-09).

``GET /api/races/{race_id}/tactics/debug`` returns the per-race
evaluation trace ring buffer written by ``tactics.pipeline`` (newest
first), the latest published call, and the live cooldown state — so
"why is the cockpit quiet?" is answerable from a phone mid-race
instead of by grepping Cloud Run logs after the fact.

Read access matches race stats: creator or any boat member (viewer
included) — a crew member debugging on the rail is the use case. 404
on missing-or-no-access (no existence leak), same pattern as
``race_stats._load_race_row``.

An EMPTY trace list for a live race is itself a diagnosis: the
telemetry handler never spawned the pipeline (free tier, or no GPS in
the batch) — every spawned evaluation writes a record, even the
cooldown-skipped ones.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import db, redis_client
from app.auth import get_current_user
from app.auth_helpers import race_read_predicate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/races", tags=["tactics-debug"])


class TacticsDebugResponse(BaseModel):
    race_id: UUID
    traces: list[dict]
    latest_call: Optional[dict]
    cooldowns: dict[str, Any]


@router.get("/{race_id}/tactics/debug", response_model=TacticsDebugResponse)
async def get_tactics_debug(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> TacticsDebugResponse:
    # ── auth: 404 on missing-or-no-access ─────────────────────────────
    pred = race_read_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT r.id FROM race_sessions r WHERE r.id = $1 AND {pred}",
            race_id, user["uid"],
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")

    # ── redis reads (non-fatal client pattern: 503 when down) ────────
    try:
        redis = redis_client.get_client()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "redis unavailable",
        ) from e

    from app.services.redis_keys import (
        tactics_cooldown_key,
        tactics_latest_key,
        tactics_trace_key,
    )
    from app.services.tactics.detectors import PRIORITY

    pipe = redis.pipeline()
    pipe.lrange(tactics_trace_key(race_id), 0, -1)
    pipe.get(tactics_latest_key(race_id))
    pipe.ttl(tactics_cooldown_key(race_id))
    call_types = sorted(PRIORITY)  # deterministic response order
    for ct in call_types:
        pipe.ttl(tactics_cooldown_key(race_id, ct))
    results = await pipe.execute()

    raw_traces, latest_blob, global_ttl = results[0], results[1], results[2]
    per_type_ttls = results[3:]

    traces = [t for t in (_parse(b) for b in raw_traces) if t is not None]
    latest_call = _parse(latest_blob)

    # Redis TTL contract: -2 = key absent, -1 = no expiry. Only a
    # positive TTL means an active cooldown; everything else reads as
    # "not cooling down" here.
    cooldowns: dict[str, Any] = {
        "global_ttl_s": global_ttl if isinstance(global_ttl, int) and global_ttl > 0 else None,
        "per_type": {
            ct: ttl
            for ct, ttl in zip(call_types, per_type_ttls)
            if isinstance(ttl, int) and ttl > 0
        },
    }

    return TacticsDebugResponse(
        race_id=race_id,
        traces=traces,
        latest_call=latest_call,
        cooldowns=cooldowns,
    )


def _parse(blob) -> Optional[dict]:
    """Best-effort JSON parse — a corrupt entry is dropped, not a 500."""
    if not blob:
        return None
    try:
        value = json.loads(blob.decode() if isinstance(blob, bytes) else blob)
        return value if isinstance(value, dict) else None
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
