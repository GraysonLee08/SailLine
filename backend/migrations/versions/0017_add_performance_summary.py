"""add performance_summary JSONB to race_sessions

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-20

Persist the Target-Actual performance summary alongside ``heel_summary``
and ``ai_summary`` so the stats view (and a future Performance Bar HUD)
can read a stable JSONB blob instead of re-replaying the track against
the polar + wind snapshot on every request.

The dict is produced by ``app.services.performance.compute_performance_summary``
at postprocess time (``workers/race_postprocess.py``), comparing the
recorded GPS track against the boat-class polar for the wind that was
present (sampled from ``race_sessions.wind_snapshot``).

Shape (mirrors ``compute_performance_summary``'s return)::

    {
      "sample_count": int,
      "avg_speed_ratio": float | None,       # actual SOG / polar target
      "avg_vmg_efficiency": float | None,    # actual VMG / target VMG
      "pct_time_on_target": float,           # 0..1, within +/-5% of polar
      "avg_target_kts": float | None,
      "avg_actual_kts": float | None,
      "by_leg": [
        { "leg_index": int, "sample_count": int,
          "avg_speed_ratio": float | None,
          "avg_vmg_efficiency": float | None }
      ]
    }

Additive-only, nullable, no FKs, no indexes — matches the conservative
pattern used for ``ai_summary``, ``wind_snapshot``, and ``heel_summary``.
Safe to apply ahead of the code change per ``docs/migrations.md``
(additive migrations go *before* the push that reads/writes them).
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN performance_summary JSONB
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            DROP COLUMN IF EXISTS performance_summary
        """
    )
