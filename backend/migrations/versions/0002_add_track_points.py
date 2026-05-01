"""add track_points table for GPS recording

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-30
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create track_points — the time-series store for GPS positions during a race.

    Schema lifted from architecture.md §9, with two tightening tweaks that
    match the conventions established in 0001_baseline:

      - `created_at` is NOT NULL (architecture.md just had a default).
      - `session_id` FK is ON DELETE CASCADE — deleting a race session
        should clean up its track points, otherwise we leak orphan rows.

    `position` uses GEOGRAPHY(POINT, 4326) so PostGIS distance/bearing
    functions return meters/degrees on the WGS84 sphere without us
    having to project. PostGIS is already installed via infra/schema.sql.

    Wind columns (`wind_speed`, `wind_dir`) are nullable — the GPS-only
    mode of the recorder doesn't capture wind, only the hardware-tier
    instrument feed does. Same column lives on `telemetry_points` (added
    in a later migration when we wire up the Pi module in v2).

    Index on (session_id, recorded_at) is the access pattern: "give me
    every point for race X in time order" — used by the playback view
    and the post-race analysis pipeline.
    """
    op.execute("""
        CREATE TABLE track_points (
            id          BIGSERIAL PRIMARY KEY,
            session_id  UUID NOT NULL REFERENCES race_sessions(id) ON DELETE CASCADE,
            recorded_at TIMESTAMPTZ NOT NULL,
            position    GEOGRAPHY(POINT, 4326) NOT NULL,
            speed_kts   FLOAT,
            heading_deg FLOAT,
            wind_speed  FLOAT,
            wind_dir    FLOAT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE INDEX track_session_time_idx
            ON track_points(session_id, recorded_at)
    """)


def downgrade() -> None:
    """Reverse 0002. Destructive — drops all recorded GPS data."""
    op.execute("DROP INDEX IF EXISTS track_session_time_idx")
    op.execute("DROP TABLE IF EXISTS track_points")
