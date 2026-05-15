"""add boat_invites table

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-15

Two flavours of invite share one table:

  * Email invite (single-use): owner enters an email; we generate a
    long random token, store it as ``code``, set ``email``, set
    ``single_use = TRUE``. The token goes into a magic link sent via
    SendGrid (see ``app/services/email.py``).

  * Join code (multi-use): owner clicks "Generate code"; we store a
    short readable code (e.g. ``RACE-XK4M``), ``email = NULL``,
    ``single_use = FALSE``. Owner shares it verbally / via text;
    anyone with the code can redeem.

``code`` is UNIQUE across the whole table — the redeem endpoint
(``POST /api/invites/redeem``) only takes a ``code`` and looks the
invite up; both flavours go through the same path.

Role is restricted to 'crew' or 'viewer' — owners can't be invited;
the boat creator is always 'owner' (backfilled in 0013).

``expires_at`` is nullable: NULL means "no expiry". Email invites
default to 7 days expiry at the application layer; join codes default
to no expiry (owner revokes manually).

``redeemed_at`` + ``redeemed_by`` populated only for single-use rows;
multi-use codes can be redeemed by many users (each creating a
``boat_crew`` row directly).
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE boat_invites (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            boat_id     UUID NOT NULL
                        REFERENCES boats(id) ON DELETE CASCADE,
            role        TEXT NOT NULL
                        CHECK (role IN ('crew', 'viewer')),
            code        TEXT NOT NULL UNIQUE,
            email       TEXT,
            single_use  BOOLEAN NOT NULL DEFAULT TRUE,
            created_by  TEXT NOT NULL
                        REFERENCES user_profiles(id) ON DELETE CASCADE,
            expires_at  TIMESTAMPTZ,
            redeemed_at TIMESTAMPTZ,
            redeemed_by TEXT
                        REFERENCES user_profiles(id) ON DELETE SET NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX boat_invites_boat_idx
            ON boat_invites(boat_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS boat_invites_boat_idx")
    op.execute("DROP TABLE IF EXISTS boat_invites")
