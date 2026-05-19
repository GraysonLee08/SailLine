"""add heel_summary JSONB to race_sessions

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-19

Persist the structured heel summary alongside ``ai_summary`` so future
UI work (per-leg heel tiles on the stats view, heel-trend dashboards
across races) can read a stable JSONB blob instead of re-running the
postprocess job from raw ``imu_samples`` every time.

Today the same dict is computed by ``app.services.heel_stats.compute_heel_summary``
and embedded into the AI-summary prompt at postprocess time, then
thrown away. By storing it on the race row we let any read endpoint
surface the numbers directly — no IMU table scan, no second LLM call.

Shape (mirrors ``compute_heel_summary``'s return)::

    {
      "sample_count": int,
      "max_heel_deg": float,        # signed; + = starboard rail down
      "max_heel_abs_deg": float,    # absolute value of the above
      "avg_heel_abs_deg": float,
      "pct_time_heeled_gt_10": float,   # 0..1
      "pct_time_heeled_gt_20": float,   # 0..1
      "max_pitch_abs_deg": float,
      "by_leg": [
        { "leg_index": int, "sample_count": int,
          "max_heel_abs_deg": float, "avg_heel_abs_deg": float }
      ]
    }

Additive-only, nullable, no FKs, no indexes — matches the conservative
pattern used for ``ai_summary`` and ``wind_snapshot``. Safe to apply
ahead of the code change per ``docs/migrations.md`` (additive migrations
go *before* the push that reads/writes them).
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN heel_summary JSONB
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            DROP COLUMN IF EXISTS heel_summary
        """
    )
