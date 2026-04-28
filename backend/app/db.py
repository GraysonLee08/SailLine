import asyncpg
from google.cloud.sql.connector import Connector, IPTypes
from app.config import settings

_connector: Connector | None = None
_pool: asyncpg.Pool | None = None


async def _getconn() -> asyncpg.Connection:
    assert _connector is not None
    return await _connector.connect_async(
        settings.cloud_sql_instance,        # "sailline:us-central1:sailline-db"
        "asyncpg",
        user=settings.db_user,              # "sailline"
        password=settings.db_password,      # from Secret Manager via env
        db=settings.db_name,                # "sailline_app"
        ip_type=IPTypes.PRIVATE,
    )


async def startup() -> None:
    global _connector, _pool
    _connector = Connector()
    _pool = await asyncpg.create_pool(
        connect=_getconn,
        min_size=1,
        max_size=5,
        command_timeout=10,
    )


async def shutdown() -> None:
    global _connector, _pool
    if _pool is not None:
        await _pool.close()
    if _connector is not None:
        await _connector.close_async()
    _pool = None
    _connector = None


def get_pool() -> asyncpg.Pool:
    assert _pool is not None, "DB pool not initialized"
    return _pool