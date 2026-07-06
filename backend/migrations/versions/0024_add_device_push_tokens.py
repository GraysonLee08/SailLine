"""add device_push_tokens for server-initiated push (dead-recorder watchdog)

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-05

First server-initiated push in the app. The dead-recorder watchdog
(``workers/recorder_watchdog.py``) needs a way to reach the sailor's
phone when the API has seen no telemetry for an open race — by
definition a moment when the app can't tell the user itself.

One row per device FCM registration token. ``token`` is the primary
key because a token IS the device identity to FCM; a token can migrate
between users (sign-out / sign-in on the same phone), which the
UPSERT in ``POST /api/users/me/push-tokens`` handles by reassigning
``user_id``. Tokens are pruned in two ways:

  * FCM returns Unregistered/invalid on send → ``app/services/push.py``
    deletes the row.
  * The user row disappears → ON DELETE CASCADE.

``platform`` is informational today (Android-only client) but
constrained now so an iOS rollout can't write garbage.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE device_push_tokens (
            token        TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL
                         REFERENCES user_profiles(id) ON DELETE CASCADE,
            platform     TEXT NOT NULL
                         CHECK (platform IN ('android', 'ios')),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX device_push_tokens_user_id_idx
            ON device_push_tokens (user_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS device_push_tokens")
