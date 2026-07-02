"""Start-line bearing resolution — wind-perpendicular line through the
start mark.

The v4 gate detector (``mark_gates.py``) models the start and finish
as a line through the start mark, perpendicular to the wind at gun
time ("the RC squares the line to the breeze"). This module owns
resolving that bearing, in strict precedence order:

  1. ``race_sessions.start_line_bearing_override`` — user-entered from
     the race editor when the RC sets something other than the
     forecast breeze. Always wins.
  2. ``race_sessions.start_line_bearing_deg`` — cached system value,
     resolved once per race by step 3 and persisted so the per-batch
     ingest hot path NEVER re-loads forecast grids.
  3. Forecast: sample u/v at the start mark at ``start_at`` from the
     same Redis-backed forecast the router uses, convert to a
     meteorological FROM direction, add 90°. Persisted to the cache
     column on success.

Resolution failure (no forecast cycle in Redis yet, unknown region,
transient Redis error) returns None — the detector falls back to v3
CPA for the start/finish marks, which is the pre-v4 behaviour. A
module-level throttle stops a failing race from hammering Redis on
every telemetry batch; the next attempt happens after
``_RETRY_INTERVAL_S``.

Conventions: bearings are degrees true, 0-360. The line's axis is
directionless (a 90° line and a 270° line are the same line) — we
store whatever falls out of the arithmetic and the gate builder
treats it symmetrically.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

from app.regions import base_region_for_point
from app.services.weather.forecast_loader import load_forecast_for_race

log = logging.getLogger(__name__)

# Minimum seconds between forecast-resolution attempts per race.
# Telemetry batches arrive every few seconds; forecast availability
# changes on ingest cadence (hourly), so 5 minutes is generous.
_RETRY_INTERVAL_S = 300.0

# Process-local throttle: {race_id: monotonic ts of last failed attempt}.
# Cloud Run may run several containers; worst case each container tries
# once per interval — still bounded and cheap.
_last_attempt: dict[UUID, float] = {}


def wind_from_deg(u: float, v: float) -> float:
    """Meteorological FROM direction (degrees true) from u/v components.

    u = eastward wind, v = northward wind. Wind blowing toward the
    south-west (u<0, v<0) comes FROM the north-east.
    """
    return math.degrees(math.atan2(-u, -v)) % 360.0


def line_bearing_from_wind(u: float, v: float) -> float:
    """Start-line axis: perpendicular to the wind."""
    return (wind_from_deg(u, v) + 90.0) % 360.0


async def resolve_start_line_bearing(
    conn: asyncpg.Connection,
    *,
    race_id: UUID,
    marks: list[dict],
    start_at: Optional[datetime],
    override: Optional[float],
    cached: Optional[float],
) -> Optional[float]:
    """Resolve the start-line bearing for a race (see module docstring).

    Never raises. Persists a freshly-computed bearing to
    ``race_sessions.start_line_bearing_deg`` in the caller's
    transaction (same row the caller already holds FOR UPDATE).
    """
    if override is not None:
        return float(override)
    if cached is not None:
        return float(cached)
    if start_at is None or not marks:
        return None

    now = time.monotonic()
    last = _last_attempt.get(race_id)
    if last is not None and now - last < _RETRY_INTERVAL_S:
        return None

    try:
        lat = float(marks[0]["lat"])
        lon = float(marks[0]["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    region = base_region_for_point(lat, lon)
    if region is None:
        _last_attempt[race_id] = now
        return None

    try:
        # 1-hour window centred on the gun is all we need; the loader
        # returns the bracketing snapshots for interpolation.
        forecast = await load_forecast_for_race(
            region.name, start_at, duration_hours=1.0
        )
        uv = forecast.sample(lat, lon, valid_time=start_at)
        if uv is None:
            _last_attempt[race_id] = now
            return None
        bearing = line_bearing_from_wind(uv[0], uv[1])
    except Exception as e:  # noqa: BLE001 — best-effort by design; includes ForecastNotAvailable
        _last_attempt[race_id] = now
        log.info(
            "start_line: bearing resolution failed for race %s: %s",
            race_id, e,
        )
        return None

    await conn.execute(
        """
        UPDATE race_sessions
        SET start_line_bearing_deg = $1,
            updated_at = NOW()
        WHERE id = $2
        """,
        bearing,
        race_id,
    )
    _last_attempt.pop(race_id, None)
    log.info(
        "start_line: resolved bearing %.1f° for race %s from %s forecast",
        bearing, race_id, region.name,
    )
    return bearing
