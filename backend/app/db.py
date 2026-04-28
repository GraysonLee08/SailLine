"""Cloud SQL connection pool.

Uses the Cloud SQL Python Connector to establish connections over the VPC's
private IP path, with asyncpg as the underlying driver.

Startup is intentionally non-fatal: if the pool can't be created (bad
credentials, network unreachable, etc.), the app still boots so /health
responds. Endpoints that need DB access will then return a clear 503.
"""

from __future__ import annotations

import logging

import asyncpg
from google.cloud.sql.connector import Connector, IPTypes

from app.config import settings

log = logging.getLogger(__name__)

_connector: Connector | None = None
_pool: asyncpg.Pool | None = None
_startup_error: str | None = None


async def _create_connection(*args, **kwargs) -> asyncpg.Connection:
    """Connection factory used by asyncpg.create_pool.

    asyncpg's pool internally passes positional args plus `loop=...` and other
    kwargs to the connect callable. We ignore them — connection details all
    come from settings via the Cloud SQL Connector.
    """
    assert _connector is not None
    return await _connector.connect_async(
        settings.cloud_sql_instance,
        "asyncpg",
        user=settings.db_user,
        password=settings.db_password,
        db=settings.db_name,
        ip_type=IPTypes.PRIVATE,
    )


async def startup() -> None:
    """Initialize the Connector and asyncpg pool. Non-fatal on failure."""
    global _connector, _pool, _startup_error
    if _pool is not None:
        return

    try:
        _connector = Connector()
        # min_size=0 → no connection is opened during pool creation;
        # the first acquire() triggers the lazy connect.
        _pool = await asyncpg.create_pool(
            connect=_create_connection,
            min_size=0,
            max_size=5,
            command_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        _startup_error = f"{type(exc).__name__}: {exc}"
        log.exception("DB pool init failed; app will boot without DB access")


async def shutdown() -> None:
    """Close the pool and Connector cleanly on app shutdown."""
    global _connector, _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _connector is not None:
        await _connector.close_async()
        _connector = None


def get_pool() -> asyncpg.Pool:
    """FastAPI dependency — returns the shared pool, or raises 503 if startup failed."""
    if _pool is None:
        from fastapi import HTTPException, status

        detail = "database pool unavailable"
        if _startup_error:
            detail = f"{detail}: {_startup_error}"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        )
    return _pool


def startup_status() -> dict:
    """Diagnostic helper — returns pool state and any startup error."""
    return {
        "pool_initialized": _pool is not None,
        "startup_error": _startup_error,
    }