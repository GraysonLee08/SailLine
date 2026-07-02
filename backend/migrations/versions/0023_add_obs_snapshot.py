"""add obs_snapshot column to race_sessions for post-race actuals

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-02

``wind_snapshot`` (0010) freezes what the FORECAST said over the race
window. ``obs_snapshot`` freezes what actually HAPPENED, as measured
by the nearest NDBC buoys / C-MAN stations to the racecourse (wind,
gusts, waves, pressure, air/water temp). Written by the
race-postprocess Cloud Run Job right after the wind snapshot step;
schema lives in ``app/services/observations/base.py``.

Provider-agnostic on purpose: each station entry carries a ``source``
tag ("ndbc" today; CO-OPS tides/currents later) so extending beyond
the Great Lakes / adding new networks needs no further migration.

Additive, NULL default, no backfill. NULL means "not yet fetched" —
the postprocess job backfills it on the next run for the race (same
semantics as heel_summary/0016). A --all --force run backfills every
finished race still inside NDBC's 45-day realtime window.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN obs_snapshot JSONB
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            DROP COLUMN IF EXISTS obs_snapshot
        """
    )
