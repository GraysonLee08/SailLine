"""add user_profiles.app_settings (tactician opt-out; future settings sync)

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-09

The tactician pipeline (2026-06-11) reads
``user_profiles.app_settings -> 'tactician' -> 'enabled'`` for its
opt-out gate — but the column was never created: the settings-sync
feature it assumed was designed, not built. The gap was invisible for
a month because the Pro tier gate meant no production user ever
reached the query; the first pro-tier evaluation (walk test,
2026-07-09) crashed with UndefinedColumnError on every run.

``DEFAULT '{}'`` keeps the pipeline's ``.get("tactician")`` semantics:
absent key = not opted out. The column is the intended home for the
mobile settings sync (GET/PUT /api/users/me/settings) when that
endpoint is actually built.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_profiles
        ADD COLUMN app_settings JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE user_profiles DROP COLUMN IF EXISTS app_settings")
