"""add user-profile fields (display_name, email, sailing & safety)

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-15

Session D4 — turn ``user_profiles`` from a tier-only stub into a real
user profile so the crew list can show names instead of raw Firebase
UIDs, and so distance-race entries (Chicago Mac, Bermuda, Transpac)
can later draw on already-captured crew credentials.

Two groups of columns, all nullable except ``profile_complete``:

  * **Identity** — ``email``, ``display_name``, ``profile_complete``,
    ``phone``, ``bio``, ``avatar_url``. The auth layer fills
    ``email`` (and ``display_name`` for Google sign-ins) automatically
    from Firebase token claims; the rest are user-edited via
    ``PATCH /api/users/me``. ``profile_complete`` gates the forced
    first-visit ProfileView in the frontend.

  * **Sailing & safety** — ``weight_lb`` (NUMERIC so we don't lose
    decimal precision on the boat-total roll-up), ``emergency_contact_*``,
    ``world_sailing_sailor_id``, ``world_sailing_category`` (CHECK
    constrained to the three IRC/ISAF groups), and
    ``safety_at_sea_cert_expiry``. These are optional fields a sailor
    fills out once and reuses across distance-race entries; the
    Race-to-Mackinac registration form is the immediate target use.

Backfill: existing rows get ``profile_complete = TRUE`` so already-
working accounts aren't yanked into the ProfileView the first time
their owner logs in after this migration ships. New rows default to
FALSE; the auth UPSERT flips them to TRUE the moment a ``name`` claim
is present (Google) or the user submits a display name (email auth).

No FKs, no indexes — these are PER-ROW attributes only ever read
by primary key.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_profiles
            ADD COLUMN email                       TEXT,
            ADD COLUMN display_name                TEXT,
            ADD COLUMN profile_complete            BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN phone                       TEXT,
            ADD COLUMN bio                         TEXT,
            ADD COLUMN avatar_url                  TEXT,
            ADD COLUMN weight_lb                   NUMERIC(5,1),
            ADD COLUMN emergency_contact_name      TEXT,
            ADD COLUMN emergency_contact_phone     TEXT,
            ADD COLUMN world_sailing_sailor_id     TEXT,
            ADD COLUMN world_sailing_category      TEXT
                CHECK (world_sailing_category IN ('group_1','group_2','group_3')),
            ADD COLUMN safety_at_sea_cert_expiry   DATE
        """
    )

    # Backfill existing rows. Anyone who already has an account at
    # migration time has been using the app fine — we don't want to
    # force them through the ProfileView on their next login. The
    # auth UPSERT will still backfill email/display_name from claims
    # on their next request, but profile_complete stays TRUE either
    # way.
    op.execute("UPDATE user_profiles SET profile_complete = TRUE")


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_profiles
            DROP COLUMN IF EXISTS safety_at_sea_cert_expiry,
            DROP COLUMN IF EXISTS world_sailing_category,
            DROP COLUMN IF EXISTS world_sailing_sailor_id,
            DROP COLUMN IF EXISTS emergency_contact_phone,
            DROP COLUMN IF EXISTS emergency_contact_name,
            DROP COLUMN IF EXISTS weight_lb,
            DROP COLUMN IF EXISTS avatar_url,
            DROP COLUMN IF EXISTS bio,
            DROP COLUMN IF EXISTS phone,
            DROP COLUMN IF EXISTS profile_complete,
            DROP COLUMN IF EXISTS display_name,
            DROP COLUMN IF EXISTS email
        """
    )
