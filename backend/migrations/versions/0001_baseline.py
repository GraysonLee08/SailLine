"""baseline — user_profiles and race_sessions as they exist in prod today

Revision ID: 0001
Revises:
Create Date: 2026-04-30
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the v1 tables.

    For a fresh database (local dev, CI, future environments), `alembic
    upgrade head` runs this and the DB ends up matching production.

    For the existing production database — which already has these tables
    from the pre-Alembic era — use `alembic stamp 0001` once instead. That
    records the revision in `alembic_version` without re-applying the DDL,
    so we don't hit `relation already exists`.

    Bootstrap pieces (PostGIS extension, role grants, default privileges)
    live in `infra/schema.sql` and run once per database before Alembic
    ever touches it. They're not part of any migration.
    """
    op.execute("""
        CREATE TABLE user_profiles (
            id              TEXT PRIMARY KEY,
            tier            TEXT NOT NULL DEFAULT 'free'
                            CHECK (tier IN ('free', 'pro', 'hardware')),
            stripe_id       TEXT UNIQUE,
            boat_class      TEXT,
            handicap_system TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE race_sessions (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
            name        TEXT NOT NULL,
            mode        TEXT NOT NULL CHECK (mode IN ('inshore', 'distance')),
            boat_class  TEXT NOT NULL,
            marks       JSONB NOT NULL DEFAULT '[]'::jsonb,
            started_at  TIMESTAMPTZ,
            ended_at    TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE INDEX race_sessions_user_idx
            ON race_sessions(user_id, created_at DESC)
    """)


def downgrade() -> None:
    """Reverse the baseline. Destructive — only useful in dev."""
    op.execute("DROP INDEX IF EXISTS race_sessions_user_idx")
    op.execute("DROP TABLE IF EXISTS race_sessions")
    op.execute("DROP TABLE IF EXISTS user_profiles")
