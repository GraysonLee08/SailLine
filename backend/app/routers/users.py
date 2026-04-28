"""User-related endpoints.

Currently contains only a DB smoke-test endpoint used to validate that the
Cloud Run revision can reach Cloud SQL via the VPC connector. Real user CRUD
arrives once Firebase JWT verification is wired up (Step B in project status).
"""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from app.db import get_pool

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me/test")
async def db_smoke_test(pool: asyncpg.Pool = Depends(get_pool)) -> dict:
    """Validate that the API can talk to Cloud SQL.

    Returns the current Postgres timestamp, the server version, and the
    PostGIS version — all from a single short-lived connection acquired
    from the pool. If any of these fail, the response is a 503 with a
    diagnostic message; the route never raises an unhandled exception.

    Remove this endpoint once real `/users/me` exists with Firebase auth.
    """
    try:
        async with pool.acquire() as conn:
            now = await conn.fetchval("SELECT NOW()")
            pg_version = await conn.fetchval("SELECT version()")
            postgis_version = await conn.fetchval("SELECT PostGIS_Version()")
    except Exception as exc:
        # Surface the cause clearly in logs but don't leak internals to clients.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"database unreachable: {type(exc).__name__}",
        ) from exc

    return {
        "db_now": now.isoformat() if now else None,
        "postgres": pg_version.split(",")[0] if pg_version else None,
        "postgis": postgis_version,
    }