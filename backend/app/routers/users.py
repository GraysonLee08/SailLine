from fastapi import APIRouter, Depends
import asyncpg
from app.db import get_pool

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me/test")
async def db_smoke_test(pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as conn:
        now = await conn.fetchval("SELECT NOW()")
        version = await conn.fetchval("SELECT version()")
        postgis = await conn.fetchval("SELECT PostGIS_Version()")
    return {
        "db_now": now.isoformat(),
        "postgres": version.split(",")[0],
        "postgis": postgis,
    }