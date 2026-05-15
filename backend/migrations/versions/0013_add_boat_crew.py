"""add boat_crew membership table

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-15

D3 introduces sharing: every boat has a list of users with a role
('owner' | 'crew' | 'viewer'). Auth across every race-scoped endpoint
flips from ``user_id = $uid`` to ``EXISTS boat_crew`` (see
``backend/app/auth_helpers.py``).

Backfill rule: every existing boat already has a single ``owner_id``
column. We INSERT one ``boat_crew`` row per boat with the owner as
'owner' so the auth refactor is non-breaking for legacy data.

PRIMARY KEY (boat_id, user_id) — a user is either on a boat or not;
their role is a single value, not a stack.

Cascades:
  * ON DELETE CASCADE both ways — deleting a boat or a user cleans
    up the membership row. Same shape as the existing FKs on
    race_sessions.user_id and boats.owner_id.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE boat_crew (
            boat_id   UUID NOT NULL
                      REFERENCES boats(id) ON DELETE CASCADE,
            user_id   TEXT NOT NULL
                      REFERENCES user_profiles(id) ON DELETE CASCADE,
            role      TEXT NOT NULL
                      CHECK (role IN ('owner', 'crew', 'viewer')),
            joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (boat_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX boat_crew_user_idx
            ON boat_crew(user_id)
        """
    )
    # Backfill: every existing boat's owner gets an 'owner' membership.
    # ON CONFLICT DO NOTHING is defensive — if someone re-runs this
    # somehow it won't double-insert.
    op.execute(
        """
        INSERT INTO boat_crew (boat_id, user_id, role)
        SELECT id, owner_id, 'owner'
        FROM boats
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS boat_crew_user_idx")
    op.execute("DROP TABLE IF EXISTS boat_crew")
