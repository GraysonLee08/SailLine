"""link boats to races and users, add per-race spinnaker flag

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-14

Three additive columns in one migration because they're a single
conceptual change (race → boat → user identity, plus the per-race
spinnaker choice that drives which of the boat's four ratings the
stats endpoint applies).

  * ``race_sessions.boat_id``
    Nullable FK to boats. Existing races stay valid with NULL — the
    legacy ``boat_class`` TEXT column on race_sessions remains the
    source of truth for routing polar lookup until D3 (when we can
    deprecate it cleanly). When boat_id is set, the stats endpoint
    uses the boat's rating; when null, no corrected time is shown.

    ON DELETE SET NULL: deleting a boat doesn't cascade-delete the
    races recorded with it (would lose track history).

  * ``user_profiles.default_boat_id``
    Nullable FK to boats. Pre-selects the boat on RaceEditor for the
    common case of one boat per user. Per-race override is supported.

    ON DELETE SET NULL: deleting the default boat just clears the
    setting.

  * ``race_sessions.uses_spinnaker``
    NOT NULL DEFAULT TRUE. Per-race choice between spinnaker and
    non-spinnaker handicap (HCP/DHCP vs NSHCP/DNSHCP). Default TRUE
    because spinnaker is the more common config in keelboat racing,
    and existing rows backfill cleanly to that.

Additive only — apply BEFORE pushing code that reads these columns.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN boat_id UUID
                REFERENCES boats(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE user_profiles
            ADD COLUMN default_boat_id UUID
                REFERENCES boats(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN uses_spinnaker BOOLEAN NOT NULL DEFAULT TRUE
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE race_sessions DROP COLUMN IF EXISTS uses_spinnaker"
    )
    op.execute(
        "ALTER TABLE user_profiles DROP COLUMN IF EXISTS default_boat_id"
    )
    op.execute(
        "ALTER TABLE race_sessions DROP COLUMN IF EXISTS boat_id"
    )
