"""create recorder_debriefs table

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-01

Background — Phase 2 of the durable upload pipeline rework
(``sailline-docs/2026-06-01_durable-upload-pipeline-plan.md``).

The mobile recorder posts a debrief blob to
``POST /api/races/{race_id}/recorder-debrief`` once per recording
session — at ``stop()``, best-effort. The blob aggregates per-session
stats (points captured / uploaded, max queue depth, http error counts,
longest success gap) and the tail of the on-device ring buffer log.

Schema decision: separate table, not a JSONB column on ``race_sessions``.

Rationale:

* History matters from day one. A race can be recorded across multiple
  sessions (user stops, restarts, or hits the auto-stop edge cases the
  Phase 1 work is intended to fix). Each session deserves its own row
  with its own ``created_at``, not an overwrite-on-stop column.
* Queries like "races where the recorder saw > 3 network errors this
  month" become a clean ``WHERE`` against a single JSONB key, with an
  index on ``session_id, created_at DESC`` answering "most recent
  debrief for race X" in one row read.
* Keeping debriefs out of ``race_sessions`` means we don't bloat the
  hot path's row size — telemetry POSTs read that row on every flush.

Shape of the ``payload`` JSONB (mirrors the mobile
``RecorderDebrief`` type, schema_version = 1)::

    {
      "schema_version": 1,
      "device": {
        "platform": "ios" | "android",
        "os_version": str | null,
        "app_version": str | null,
        "build_id": str | null      // EAS build id when available
      },
      "session": {
        "start_ts": iso8601 str,
        "end_ts":   iso8601 str,
        "duration_s": int
      },
      "capture": {
        "points_captured": int,
        "points_uploaded": int,
        "points_remaining_in_queue": int,
        "max_queue_depth": int
      },
      "uploads": {
        "attempts": int,
        "successes": int,
        "http_5xx": int,
        "http_4xx": int,
        "network_errors": int,
        "longest_success_gap_s": float
      },
      "recent_log": [ RecorderLogEntry, ... up to 50 ]
    }

Cascade on race_sessions delete: a race row deletion cascades to its
debriefs. The debrief is meaningless without the race it describes,
and we never want orphan rows. (race_sessions itself is rarely deleted,
but the constraint costs nothing.)

Additive — no impact on existing tables, indexes, or routers. Safe to
apply ahead of the matching code push per docs/migrations.md.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE recorder_debriefs (
            id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id  UUID        NOT NULL
                                    REFERENCES race_sessions(id)
                                    ON DELETE CASCADE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payload     JSONB       NOT NULL
        )
        """
    )

    # "Latest debrief for race X" — the most common read pattern (the
    # post-race stats view, the planned admin view). DESC index on
    # created_at lets ORDER BY ... LIMIT 1 do a single index lookup.
    op.execute(
        """
        CREATE INDEX recorder_debriefs_session_created_idx
            ON recorder_debriefs (session_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS recorder_debriefs_session_created_idx"
    )
    op.execute("DROP TABLE IF EXISTS recorder_debriefs")
