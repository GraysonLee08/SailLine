"""add start-line bearing columns to race_sessions for v4 gate detection

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-02

Background — 2026-07-01 Beer Can 7.1.2026:

The start "mark" is a single point (the committee/pin mark); the actual
start line extends an RC-determined distance from that mark,
perpendicular to the wind. The v3 CPA detector modelled the start as a
point + 250 m radius, so a perfectly normal start crossing ~390 m from
the mark never registered, the sequential detector wedged on mark 0,
every downstream mark read as "missed", auto-finish never fired, and
the entire post-race pipeline (gated on ended_at) never ran.

The v4 detector (mark_gates.py) models the start and finish as a LINE
through the mark, perpendicular to the wind at gun time, extending
LINE_HALF_LEN_M to each side. That line needs a bearing:

  * ``start_line_bearing_override`` — user-entered compass bearing of
    the line's axis (degrees true, 0-360). Set from the race editor
    when the RC squares the line to something other than the forecast
    breeze. NULL = no override, derive from forecast.
  * ``start_line_bearing_deg`` — system-resolved cache. Written once by
    the ingest path the first time the v4 detector needs the line
    (forecast wind at the start mark at gun time, +90°). Cached so the
    per-batch hot path never re-loads forecast grids from Redis. NULL =
    not yet resolved (resolver retries with a throttle) — the detector
    falls back to v3 CPA for the start mark until a bearing exists.

Rounding side ("leave mark to port/starboard") needs NO migration —
marks live in the ``marks`` JSONB column, which tolerates extra keys
(``rounding: "port" | "starboard"``) by design.

Additive, NULL defaults, no backfill. Existing races keep CPA
behaviour until they have a bearing.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            ADD COLUMN start_line_bearing_override DOUBLE PRECISION,
            ADD COLUMN start_line_bearing_deg DOUBLE PRECISION
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE race_sessions
            DROP COLUMN IF EXISTS start_line_bearing_override,
            DROP COLUMN IF EXISTS start_line_bearing_deg
        """
    )
