"""add rounding key to existing race_sessions.marks entries

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-13

The marks column is JSONB so technically no schema change is needed for
this feature — the multi-leg engine treats absent ``rounding`` as None
(no constraint). This migration backfills ``rounding: null`` on every
existing mark so:

  1. The frontend can safely read ``mark.rounding`` everywhere without
     defensive None checks scattered through the editor.
  2. Future migrations have a stable shape to operate on.
  3. ``race_sessions`` rows written before v9 look identical to those
     written by v9 — important for the test harness and for any future
     "show old route history" UI.

This is intentionally a data-only migration. No DDL.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``rounding: null`` to every mark that doesn't already have it.

    Safe to re-run: the EXISTS clause short-circuits rows where every
    mark already carries the key, so this is idempotent at the row
    level. Non-array marks (or NULL) are left untouched.
    """
    op.execute(
        """
        UPDATE race_sessions
        SET marks = (
            SELECT jsonb_agg(
                CASE
                    WHEN mark ? 'rounding' THEN mark
                    ELSE mark || jsonb_build_object('rounding', NULL)
                END
            )
            FROM jsonb_array_elements(marks) AS mark
        )
        WHERE marks IS NOT NULL
          AND jsonb_typeof(marks) = 'array'
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(marks) AS m
              WHERE NOT (m ? 'rounding')
          );
        """
    )


def downgrade() -> None:
    """Strip the ``rounding`` key from every mark.

    Dev convenience only. In production, downgrading is rare and a user
    who had set a non-null rounding will lose that data. Acceptable for
    a v1.x rollback.
    """
    op.execute(
        """
        UPDATE race_sessions
        SET marks = (
            SELECT jsonb_agg(mark - 'rounding')
            FROM jsonb_array_elements(marks) AS mark
        )
        WHERE marks IS NOT NULL
          AND jsonb_typeof(marks) = 'array';
        """
    )
