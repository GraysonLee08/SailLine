"""add wind_snapshot to race_sessions

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-14

Stores a frozen copy of the wind forecast that was active during the
race, so wind-vs-track analysis works regardless of how old the race
is.

Why we need this column:
  * Live forecasts in Redis expire fast — HRRR after 2h, GFS after 12h
    (see ``workers/weather_ingest.py``). A user opening their stats
    view the next morning would have no wind data to compare against
    their track.
  * Persisting a snapshot makes the AI summary's wind context
    deterministic across regenerations and decouples post-race
    analysis from the live ingest pipeline.
  * Sized small enough to live on the row: ~5-20 KB per race for a
    typical buoy course (10 km grid, 15-min step over a 2-3 hour
    window). Sailline's race volume is low enough that this doesn't
    move the needle on row size.

Shape::

    {
        "bbox":      [min_lat, min_lon, max_lat, max_lon],
        "grid_deg":  0.1,
        "t_start":   "2026-05-13T18:00:00Z",
        "t_end":     "2026-05-13T21:00:00Z",
        "dt_s":      900,
        "source":    "hrrr+conus",
        "samples":   [
            {"t_idx": 0, "lat": 42.0, "lon": -87.5, "dir_deg": 210, "speed_kt": 8.4},
            ...
        ]
    }

Optionally compressed (gzip + base64) if uncompressed payload exceeds
50 KB — that path lives in ``services/wind_snapshot.py``.

Nullable because:
  * Historical races (pre-Session D1) have no snapshot.
  * Forecast may not be available at the moment auto-stop fires
    (Redis miss, ingest gap) — the job logs and proceeds without it
    rather than retrying forever.

Following the migrations.md runbook: additive only — apply BEFORE
pushing the code that writes the column. Force a Cloud Run revision
rollover with ``--update-env-vars=BUMP=$(date +%s)`` after applying.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add wind_snapshot JSONB NULL on race_sessions."""
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN wind_snapshot JSONB
        """
    )


def downgrade() -> None:
    """Drop the column. Loses any stored snapshots."""
    op.execute(
        "ALTER TABLE race_sessions DROP COLUMN IF EXISTS wind_snapshot"
    )
