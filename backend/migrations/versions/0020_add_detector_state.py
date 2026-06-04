"""add detector_state to race_sessions for cross-batch mark detection

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-04

Background — 2026-06-03 Beer Can Race 4:

Mark 2 (mid-course buoy) was missed by the v3 detector despite a textbook
1.1 m closest-point-of-approach with five consecutive strictly-increasing
distance samples immediately after (the canonical depart pattern).

Root cause:

``MarkRoundingDetector`` holds traversal state on the instance —
``_last_dist``, ``_min_dist``, ``_min_ts``, ``_departing`` — and that
state is reset on every batch because ``detect_and_persist_new_passes``
constructs a fresh detector per call (see
``backend/app/services/track_ingest.py``). The module docstring of
``mark_rounding.py`` explicitly acknowledges this gap:

    Traversal state (running minimum, last distance, departing count)
    is intentionally NOT resumed across batches.

With the mobile native uploader's ``autoSyncThreshold: 1`` setting,
batches arrive containing 1-3 samples each. The CPA sample lands in
batch K; the depart-confirm samples land in batches K+1, K+2, K+3.
``_last_dist`` is ``None`` at the start of each batch, so the
``elif self._last_dist is not None and d > self._last_dist`` branch
that increments ``_departing`` never fires. The detector watches the
target forever and never emits.

Marks 0, 1, 3 detected correctly because they happened to coincide
with multi-sample batches (offline catch-up flushes, brief connectivity
drops). Real-time detection is currently a coin flip dependent on
upload cadence — which is why the user had to tap manually on Mark 2
mid-race.

Fix:

Add ``detector_state JSONB`` to ``race_sessions``. The track-ingest
service writes the running ``{last_dist, min_dist, min_ts, min_lat,
min_lon, departing}`` after every batch and restores it on the next
batch's detector construction. The persistence lives in the same
UPDATE that already writes ``mark_passes`` (no extra round-trip).

Additive — defaults to NULL. The detector treats NULL as "fresh
traversal," matching today's behaviour for the very first batch of a
race.

Forward-compatible: state-dependent fields are stored under explicit
keys, so a future v4 detector with extra state can add keys without
a migration. NULL is the "no state" sentinel.

No data migration required. Existing in-flight races finish their
current batch in the pre-fix code path (the column reads as NULL,
detector starts fresh), and pick up the new persistence on the next
batch. The contract is one-way idempotent: once the column is set, the
detector restores from it; once it's cleared (at race completion the
postprocess job nulls it), the detector starts fresh again.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # JSONB chosen over a dedicated columns-per-field schema for two
    # reasons:
    #   * The state shape is a detector implementation detail; adding
    #     a v4 detector with extra state (e.g. recent-bearing history)
    #     shouldn't need another migration.
    #   * The column is hot-path read+write on every telemetry batch —
    #     a single JSONB column with one row update is cheaper than
    #     N column updates on the same row.
    #
    # NULL default matches the "fresh traversal" semantic. No backfill
    # needed — every race starts with no traversal state, and the
    # detector handles NULL as "start clean."
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN detector_state JSONB
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            DROP COLUMN IF EXISTS detector_state
        """
    )
