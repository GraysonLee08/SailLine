"""add start_at to race_sessions for race-day countdown

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-01
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add nullable start_at to race_sessions.

    This is the gun time for the race — a single TIMESTAMPTZ rather than
    paired date + time columns. Pair-of-columns would force us to handle
    a four-state matrix (both set / only date / only time / neither) for
    no real gain, since the start gun is one moment in time.

    The frontend builds the timestamp from local date + time inputs and
    sends it as ISO UTC. Storage is timezone-aware, so the same race
    displays correctly whether the user is in CDT, EDT, or somewhere
    else when they look at it.

    Nullable so existing rows continue to validate, and so users can
    save a course before scheduling is finalized — the countdown UI
    treats null as "no start time set" rather than an error.
    """
    op.execute("""
        ALTER TABLE race_sessions
            ADD COLUMN start_at TIMESTAMPTZ
    """)


def downgrade() -> None:
    """Reverse 0003. Drops the column and any data in it."""
    op.execute("ALTER TABLE race_sessions DROP COLUMN IF EXISTS start_at")
