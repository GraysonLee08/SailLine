"""add unique constraints for idempotent telemetry ingest

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-01

Background — 2026-05-31 on-water race:

The mobile recorder went silent for stretches of the race and then dumped
buffered GPS in bursts. The /api/races/{id}/telemetry endpoint inserts
without dedup, and its docstring explicitly accepts duplicate rows from a
re-sent batch ("not idempotent on duplicate flushes"). Combined with the
durable-queue design now landing in Phase 4 of the recorder rework, the
client will need to retry batches whose response was lost in flight; that
retry must NOT produce duplicate rows.

This migration adds UNIQUE (session_id, recorded_at) on both telemetry
tables. The matching ON CONFLICT DO NOTHING change ships in the same
session in app/routers/telemetry.py and app/routers/tracks.py (the legacy
/track endpoint shares the same table). Together they make ingest
idempotent: a re-sent batch with the same sample timestamps inserts the
new rows and skips the duplicates, and the server reports `gps_inserted`
as the actual landed count.

Precondition — duplicates must be cleared before this migration runs.
Operator MUST execute the dedupe query in the runbook
(sailline-docs/2026-06-01_durable-upload-pipeline-plan.md §5.1 dedupe
section, mirrored to the migrations runbook) BEFORE `alembic upgrade
head`. Without dedup, the ALTER TABLE … ADD CONSTRAINT statement fails.

Additive-only otherwise. No FKs, no column changes. The new indexes are
implicit (UNIQUE constraint creates a backing btree on each table); they
also speed the (session_id, recorded_at) range scans the playback view
runs at GET time, so they pay back independently of the dedup contract.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # track_points: one row per (race session, GPS sample timestamp).
    # A re-sent batch from the recorder's offline queue arrives with the
    # same sample timestamps as the first send; the unique key on
    # recorded_at makes the second send a no-op. The recorder MUST keep
    # ms precision on the timestamp it sends (the column is timestamptz)
    # so two real samples from the same race never collide.
    op.execute(
        """
        ALTER TABLE track_points
            ADD CONSTRAINT track_points_session_recorded_uniq
            UNIQUE (session_id, recorded_at)
        """
    )

    # imu_samples: same contract. IMU is captured at higher rate
    # (~20 Hz target) so two samples per 50 ms are common — the
    # timestamptz column has microsecond precision, so ms-distinct
    # samples will not collide. If the client ever rounds to seconds
    # the constraint catches it as data loss, which is the right
    # signal to surface rather than to silently drop.
    op.execute(
        """
        ALTER TABLE imu_samples
            ADD CONSTRAINT imu_samples_session_recorded_uniq
            UNIQUE (session_id, recorded_at)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE imu_samples
            DROP CONSTRAINT IF EXISTS imu_samples_session_recorded_uniq
        """
    )
    op.execute(
        """
        ALTER TABLE track_points
            DROP CONSTRAINT IF EXISTS track_points_session_recorded_uniq
        """
    )
