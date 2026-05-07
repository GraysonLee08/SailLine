"""add imu_samples, race_calibrations, and gps accuracy column

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-07
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add IMU sample storage, calibration history, and GPS accuracy.

    Three changes in one migration because they all back the new
    POST /api/races/{id}/telemetry endpoint:

    1. ``track_points.gps_acc_m``: 95% accuracy radius from the
       browser's Geolocation API. Nullable so existing rows from the
       old /track endpoint stay valid. Routing/analysis queries can
       filter low-quality fixes (e.g. acc > 25 m) at read time.

    2. ``imu_samples``: high-rate (10-20 Hz) heel/pitch/yaw stream.
       Kept separate from ``track_points`` because the rate disparity
       (track is GPS-1Hz) would mean joining ~15-20 rows of NULL
       fields per second under a bundled approach. The
       (session_id, recorded_at) index serves both per-session queries
       and time-window joins from the routing/replay pipelines.

    3. ``race_calibrations``: heel/pitch zero-offset history. We store
       *raw* IMU values in ``imu_samples`` and apply offsets at read
       time. A new row is appended when the sailor re-zeroes; older
       rows are not modified. The latest row with
       ``captured_at <= sample.t`` is the active calibration for that
       sample. Keeping history (rather than overwriting a single
       column on ``race_sessions``) preserves the ability to
       reprocess older data if a calibration is later found to have
       been bad — same principle as storing IMU values uncorrected.

    Sign conventions are documented on the Pydantic models in
    ``app/routers/telemetry.py`` (the source of truth for clients).
    """
    op.execute("""
        ALTER TABLE track_points
            ADD COLUMN gps_acc_m REAL
    """)

    op.execute("""
        CREATE TABLE imu_samples (
            id          BIGSERIAL    PRIMARY KEY,
            session_id  UUID         NOT NULL
                REFERENCES race_sessions(id) ON DELETE CASCADE,
            recorded_at TIMESTAMPTZ  NOT NULL,
            heel_deg    REAL         NOT NULL,
            pitch_deg   REAL         NOT NULL,
            yaw_deg     REAL         NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX idx_imu_samples_session_time
            ON imu_samples (session_id, recorded_at)
    """)

    op.execute("""
        CREATE TABLE race_calibrations (
            id                     BIGSERIAL    PRIMARY KEY,
            session_id             UUID         NOT NULL
                REFERENCES race_sessions(id) ON DELETE CASCADE,
            captured_at            TIMESTAMPTZ  NOT NULL,
            heel_zero_offset_deg   REAL         NOT NULL,
            pitch_zero_offset_deg  REAL         NOT NULL,
            created_at             TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX idx_race_calibrations_session_time
            ON race_calibrations (session_id, captured_at DESC)
    """)


def downgrade() -> None:
    """Reverse 0004. Drops the IMU/calibration tables and accuracy column."""
    op.execute("DROP TABLE IF EXISTS race_calibrations")
    op.execute("DROP TABLE IF EXISTS imu_samples")
    op.execute("ALTER TABLE track_points DROP COLUMN IF EXISTS gps_acc_m")
