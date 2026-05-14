"""add mark_passes to race_sessions

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-14

Stores the authoritative server-computed list of mark roundings for each
race. Shape: ``[{mark_index, ts, lat, lon}, ...]`` in chronological
order.

Why a column instead of recomputing on demand:
  * Track ingest is incremental — every batch of points needs to be
    fed through the detector against the *full* prior history. Reading
    the prior pass list is O(passes), much cheaper than re-running the
    detector over the whole track on every flush.
  * The post-race stats endpoint (Session D) needs leg splits derived
    from these timestamps; reading a column is cheaper than re-running
    the detector at view time.
  * The auto-stop recorder reads the live count via the POST response —
    the column is the source of truth, the response is a hint.

Why JSONB, not a side table:
  * Race-scoped, never queried across races, never indexed inside.
  * Always read and rewritten as a whole list (no row-level updates).
  * Matches the existing ``marks`` JSONB pattern in the same row.

Following the migrations.md runbook: additive only — apply BEFORE
pushing the code that reads the column. Force a Cloud Run revision
rollover with ``--update-env-vars=BUMP=$(date +%s)`` after applying.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add mark_passes JSONB NOT NULL DEFAULT '[]'::jsonb."""
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN mark_passes JSONB NOT NULL DEFAULT '[]'::jsonb
        """
    )


def downgrade() -> None:
    """Drop the column. Loses any recorded mark passes."""
    op.execute(
        "ALTER TABLE race_sessions DROP COLUMN IF EXISTS mark_passes"
    )
