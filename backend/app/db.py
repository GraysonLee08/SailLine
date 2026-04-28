"""Cloud SQL connection pool.

Uses the Cloud SQL Python Connector to establish connections over the VPC's
private IP path, with asyncpg as the underlying driver. The Connector handles
TLS and credential rotation; asyncpg.Pool handles connection reuse.

Lifecycle is managed by the FastAPI lifespan context in `app.main` —
`startup()` is called on app boot, `shutdown()` on graceful termination.
"""

from __future__ import annotations

import asyncpg
from google.cloud.sql.connector import Connector, IPTypes

from app.config import settings

_connector: Connector | None = None
_pool: asyncpg.Pool | None = None


async def _create_connection() -> asyncpg.Connection:
    """Connection factory passed to asyncpg.create_pool.

    Each new pool member is established via the Cloud SQL Connector, which
    resolves the instance, opens a private-IP connection, and wraps it in
    asyncpg's protocol.
    """
    assert _connector is not None, "Connector not initialized — call startup() first"
    return await _connector.connect_async(
        settings.cloud_sql_instance,
        "asyncpg",
        user=settings.db_user,
        password=settings.db_password,
        db=settings.db_name,
        ip_type=IPTypes.PRIVATE,
    )


async def startup() -> None:
    """Initialize the Cloud SQL Connector and the asyncpg pool.

    Called from the FastAPI lifespan handler at app start. Idempotent — safe
    to call twice (e.g., during a hot reload in local dev).
    """
    global _connector, _pool
    if _pool is not None:
        return

    _connector = Connector()
    _pool = await asyncpg.create_pool(
        connect=_create_connection,
        min_size=1,
        max_size=5,
        command_timeout=10,
    )


async def shutdown() -> None:
    """Close the pool and the Connector cleanly on app shutdown."""
    global _connector, _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _connector is not None:
        await _connector.close_async()
        _connector = None


def get_pool() -> asyncpg.Pool:
    """FastAPI dependency — returns the shared pool.

    Usage in a router:
        from fastapi import Depends
        from app.db import get_pool
        import asyncpg

        @router.get("/something")
        async def handler(pool: asyncpg.Pool = Depends(get_pool)):
            async with pool.acquire() as conn:
                ...
    """
    assert _pool is not None, "DB pool not initialized — startup() was not called"
    return _pool