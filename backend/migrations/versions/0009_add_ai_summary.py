"""add ai_summary to race_sessions

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-14

Stores the Claude-generated post-race recap and coaching tips, plus the
prompt version that produced it so we can invalidate cached summaries
when the prompt template changes.

Shape::

    {
        "recap": "string — 2-3 paragraph narrative recap of the race",
        "tips":  ["string — actionable coaching bullet", ...],
        "model": "claude-haiku-4-5-20251001",
        "prompt_version": 1,
        "generated_at": "2026-05-14T18:30:00Z"
    }

Nullable because:
  * Historical races (pre-Session D1) have no summary.
  * Live races have no summary until the Cloud Run Job
    ``race-postprocess`` finishes — the stats endpoint can return stats
    with ``summary: null`` while the job is still running.
  * If the Anthropic call fails (rate limit, transient error) we still
    want stats to load — the column stays null and the user can hit
    "Regenerate".

Why JSONB, not a side table:
  * One row per race. Never queried across races. Always read and
    rewritten as a whole. Same pattern as ``marks`` and ``mark_passes``.

Why store the model name and prompt version:
  * When we tune the prompt we bump ``PROMPT_VERSION`` in
    ``app/services/race_summary.py``. The Cloud Run Job compares the
    stored value to the current constant; mismatch triggers
    regeneration on the next stats fetch (or on explicit user
    "Regenerate"). The model string is metadata for support / future
    A/B comparisons.

Following the migrations.md runbook: additive only — apply BEFORE
pushing the code that writes the column. Force a Cloud Run revision
rollover with ``--update-env-vars=BUMP=$(date +%s)`` after applying.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ai_summary JSONB NULL on race_sessions."""
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN ai_summary JSONB
        """
    )


def downgrade() -> None:
    """Drop the column. Loses any generated summaries."""
    op.execute(
        "ALTER TABLE race_sessions DROP COLUMN IF EXISTS ai_summary"
    )
