"""add auto_start_enabled to race_sessions

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-14

Additive boolean column controlling whether the frontend recorder should
auto-start at race_start - 5min. Defaults to TRUE so the behaviour is
on for every existing race without an explicit migration; users can flip
it OFF per-race in the editor.

Why a schema column instead of localStorage:
  * Setting survives device swap (sail one race from a phone, view it
    from a laptop — same intent applies).
  * The route_recompute worker may want to read it later (e.g. to gate a
    pre-race notification ping); a server-side column is the natural
    place for that.
  * Cost is one bool; trivial.

Following the migrations.md runbook: additive only — apply BEFORE
pushing the code that reads the column. asyncpg's prepared-statement
cache will pick up the new column on the next pool init; force a Cloud
Run revision rollover with `--update-env-vars=BUMP=$(date +%s)` after
applying, per the same runbook.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add auto_start_enabled BOOL NOT NULL DEFAULT TRUE."""
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN auto_start_enabled BOOLEAN NOT NULL DEFAULT TRUE
        """
    )


def downgrade() -> None:
    """Drop the column. Loses any per-race opt-out the user set."""
    op.execute(
        "ALTER TABLE race_sessions DROP COLUMN IF EXISTS auto_start_enabled"
    )
