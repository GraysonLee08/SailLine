"""add tactician_calls table for the in-race AI tactician

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-11

Spec: ``sailline -docs/2026-06-11_ai-tactician-spec.md``.

The tactician publishes plain-language calls ("tack in about 2 minutes
at the layline", "traveler down — you're at 26° heel") to the phone
mid-race. Every call that is actually DELIVERED is persisted here so:

* the post-race Review screen can show "calls made vs what happened",
* the recap prompt can reference the calls in the debrief, and
* on-water threshold tuning can correlate call quality with the
  detector numbers (``diagnosis``) and the prompt that produced the
  wording (``prompt_version``).

A dedicated table (not a JSONB column on race_sessions) because calls
are append-only rows queried independently of the session row — the
Review screen wants "all calls for race X ordered by time," and the
pipeline wants "last 3 calls" on every evaluation, neither of which a
JSONB array on the hot session row serves well.

Dropped/SILENT calls are intentionally NOT persisted (they're logged);
the table is the record of what the crew actually heard.

Additive only. Apply via ``alembic upgrade head`` before deploying the
API revision that writes to it (the tactician pipeline INSERTs here).
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tactician_calls (
            id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id      UUID        NOT NULL
                            REFERENCES race_sessions(id) ON DELETE CASCADE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            call_type       TEXT        NOT NULL,
            call_class      TEXT        NOT NULL,
            eta             TIMESTAMPTZ,
            message         TEXT        NOT NULL,
            diagnosis       JSONB,
            model           TEXT,
            prompt_version  INTEGER
        )
        """
    )
    # The two real access patterns: "calls for this race in order"
    # (Review screen, last-3 lookup) — one composite index covers both.
    op.execute(
        """
        CREATE INDEX idx_tactician_calls_session_time
            ON tactician_calls (session_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tactician_calls")
