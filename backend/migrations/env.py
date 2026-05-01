"""Alembic migration environment.

Builds the DB URL from environment variables instead of `alembic.ini`. This
lets the same config work for:

  - Local dev (cloud-sql-proxy listening on 127.0.0.1)
  - Cloud Shell migrations (cloud-sql-proxy listening on 127.0.0.1)
  - Direct private-IP from inside the VPC (set DB_HOST to the instance IP)

We use `psycopg` (sync) for migrations even though the runtime app uses
`asyncpg`. Migrations run rarely, from a shell — sync is simpler and avoids
mixing event loops with Alembic's CLI.

Required env vars:
    DB_USER, DB_PASSWORD, DB_NAME

Optional:
    DB_HOST  (default 127.0.0.1)
    DB_PORT  (default 5432)
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Alembic Config object — gives access to alembic.ini values.
config = context.config

# Set up loggers from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Migrations are written as raw SQL via op.execute(). No ORM models exist
# in this project (the runtime app uses asyncpg + raw SQL), so
# target_metadata stays None and Alembic's --autogenerate is disabled
# by design. Every migration is hand-written and explicit.
target_metadata = None


def _build_url() -> str:
    """Construct a SQLAlchemy URL for psycopg from env vars.

    Fails loudly if required vars are missing — better to crash here than
    to attempt a migration against the wrong database.
    """
    try:
        user = os.environ["DB_USER"]
        password = os.environ["DB_PASSWORD"]
        name = os.environ["DB_NAME"]
    except KeyError as missing:
        raise RuntimeError(
            f"Alembic env: missing required env var {missing}. "
            "Set DB_USER, DB_PASSWORD, and DB_NAME before running migrations."
        ) from None

    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = os.environ.get("DB_PORT", "5432")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database.

    Triggered by `alembic upgrade head --sql`. Useful for review before
    applying anything destructive.
    """
    context.configure(
        url=_build_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the DB and apply migrations."""
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _build_url()
    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        # NullPool: open a connection, run migrations, close it. Don't
        # leave anything lingering — Alembic exits when this returns.
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
