"""Async Redis client lifecycle.

Mirrors db.py: non-fatal startup so /health stays responsive even if Memorystore
is unreachable. Endpoints that need Redis raise 503 when the client is missing.
"""
from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.config import settings

log = logging.getLogger(__name__)

_client: Redis | None = None
_startup_error: str | None = None


async def startup() -> None:
    global _client, _startup_error
    if _client is not None:
        return
    if not settings.redis_host:
        _startup_error = "REDIS_HOST not configured"
        log.warning("Redis client not initialized: REDIS_HOST missing")
        return
    try:
        # Don't ping eagerly — first GET triggers the connection.
        _client = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    except Exception as exc:  # noqa: BLE001
        _startup_error = f"{type(exc).__name__}: {exc}"
        log.exception("Redis client init failed; app will boot without cache")


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_client() -> Redis:
    if _client is None:
        from fastapi import HTTPException, status
        detail = "redis client unavailable"
        if _startup_error:
            detail = f"{detail}: {_startup_error}"
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)
    return _client