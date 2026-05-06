# backend/app/services/weather/forecast_loader.py
"""Load a WindForecast for a race from cached HRRR + GFS cycles.

Cycle selection rule (per product spec):

    HRRR_HORIZON_HOURS = 18
    If race_start <= now + HRRR_HORIZON_HOURS:
        - HRRR for [race_start, hrrr_cycle_ref + 18h]
        - GFS for (hrrr_cycle_ref + 18h, race_end] if race extends beyond
        - "Latest cycle period" - newest HRRR cycle, regardless of whether
          its F00 falls before or after race_start. For a race in 90min,
          the latest cycle's F02 lands roughly on the gun.
    Else:
        Raise ForecastNotAvailable. Frontend shows
        'forecast available in N hours; route will populate then.'

Race duration defaults to 6h if the caller didn't supply one - long enough
to be useful for the common inshore/distance case, short enough to avoid
loading hundreds of GFS fhours for a buoy race.
"""
from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import redis_client
from app.regions import REGIONS
from app.services.routing.isochrone import WindField
from app.services.routing.wind_forecast import WindForecast, _parse_iso

log = logging.getLogger(__name__)

HRRR_HORIZON_HOURS = 18
DEFAULT_RACE_DURATION_HOURS = 6.0
GFS_HORIZON_HOURS = 120  # matches workers/weather_ingest.SOURCES["gfs"].fhour_max


class ForecastNotAvailable(Exception):
    """Raised when no cycle covers the requested race window.

    Attributes
    ----------
    available_at : datetime
        UTC moment when a covering cycle is expected to be ingested.
    hours_until_available : float
        Convenience for the API to surface to the frontend.
    """
    def __init__(self, available_at: datetime, reason: str = "") -> None:
        self.available_at = available_at
        self.hours_until_available = max(
            0.0, (available_at - datetime.now(timezone.utc)).total_seconds() / 3600.0
        )
        super().__init__(
            f"forecast not yet available; expected at {available_at.isoformat()} "
            f"({self.hours_until_available:.1f}h). {reason}".strip()
        )


@dataclass
class _CycleInfo:
    cycle_iso: str
    reference_time: datetime
    fhours: list[int]
    valid_times: list[datetime]


async def _newest_cycle(source: str, region: str) -> Optional[_CycleInfo]:
    redis = redis_client.get_client()
    cycles_key = f"weather:{source}:{region}:cycles"
    # ZRANGE with REV returns newest first.
    raw = await redis.zrevrange(cycles_key, 0, 0)
    if not raw:
        return None
    cycle_iso = raw[0].decode() if isinstance(raw[0], bytes) else raw[0]

    manifest_key = f"weather:{source}:{region}:{cycle_iso}:manifest"
    manifest_blob = await redis.get(manifest_key)
    if manifest_blob is None:
        log.warning("cycles index has %s but manifest missing", cycle_iso)
        return None
    manifest = json.loads(manifest_blob)
    return _CycleInfo(
        cycle_iso=cycle_iso,
        reference_time=_parse_iso(manifest["reference_time"]),
        fhours=manifest["fhours"],
        valid_times=[_parse_iso(t) for t in manifest["valid_times"]],
    )


async def _load_snapshot(source: str, region: str, cycle_iso: str, fhour: int) -> WindField:
    redis = redis_client.get_client()
    key = f"weather:{source}:{region}:{cycle_iso}:f{fhour:03d}"
    blob = await redis.get(key)
    if blob is None:
        raise RuntimeError(f"missing forecast snapshot: {key}")
    payload = json.loads(gzip.decompress(blob))
    return WindField.from_payload(payload)


async def load_forecast_for_race(
    region: str,
    race_start: datetime,
    duration_hours: float = DEFAULT_RACE_DURATION_HOURS,
) -> WindForecast:
    """Build a time-aware forecast for the race window.

    Raises
    ------
    ForecastNotAvailable
        Race starts outside the HRRR horizon - caller should surface
        the wait-time to the user.
    RuntimeError
        Cycles index is empty (no ingest has run yet) or a referenced
        snapshot blob is missing. Operational bug, not user-facing.
    """
    if region not in REGIONS:
        raise ValueError(f"unknown region: {region}")
    region_obj = REGIONS[region]

    if race_start.tzinfo is None:
        race_start = race_start.replace(tzinfo=timezone.utc)
    race_end = race_start + timedelta(hours=duration_hours)
    now = datetime.now(timezone.utc)

    has_hrrr = "hrrr" in region_obj.sources
    has_gfs = "gfs" in region_obj.sources

    # Step 1 - HRRR window check. The user-facing rule is "race must start
    # within the HRRR horizon, or we don't route yet."
    if has_hrrr:
        hrrr_window_end = now + timedelta(hours=HRRR_HORIZON_HOURS)
        if race_start > hrrr_window_end:
            # Notify mode: forecast becomes available when a future HRRR
            # cycle's F18 reaches race_start. Each new cycle is 1h.
            available_at = race_start - timedelta(hours=HRRR_HORIZON_HOURS)
            raise ForecastNotAvailable(
                available_at=available_at,
                reason="race starts beyond HRRR forecast horizon",
            )

    # Step 2 - load the newest cycles available.
    hrrr_cycle = await _newest_cycle("hrrr", region) if has_hrrr else None
    gfs_cycle = await _newest_cycle("gfs", region) if has_gfs else None

    if hrrr_cycle is None and gfs_cycle is None:
        raise RuntimeError(f"no ingested cycles for region={region}")

    # Step 3 - pick fhours that cover [race_start, race_end].
    snapshots: list[WindField] = []
    quality_parts: list[str] = []

    hrrr_max_valid: Optional[datetime] = None
    if hrrr_cycle is not None:
        # Keep HRRR fhours whose valid_time intersects the race window,
        # plus one fhour on each side so interpolation has bracketing.
        hrrr_picks = _pick_bracketing(
            hrrr_cycle.fhours, hrrr_cycle.valid_times, race_start, race_end,
        )
        for fh in hrrr_picks:
            snapshots.append(await _load_snapshot("hrrr", region, hrrr_cycle.cycle_iso, fh))
        if hrrr_picks:
            quality_parts.append("hrrr")
            hrrr_max_valid = hrrr_cycle.valid_times[hrrr_cycle.fhours.index(hrrr_picks[-1])]

    if gfs_cycle is not None and (hrrr_max_valid is None or race_end > hrrr_max_valid):
        # Cover the tail past HRRR with GFS.
        gfs_window_start = hrrr_max_valid or race_start
        gfs_picks = _pick_bracketing(
            gfs_cycle.fhours, gfs_cycle.valid_times, gfs_window_start, race_end,
        )
        for fh in gfs_picks:
            snapshots.append(await _load_snapshot("gfs", region, gfs_cycle.cycle_iso, fh))
        if gfs_picks:
            quality_parts.append("gfs")

    if not snapshots:
        # Cycles existed but no fhour covered the window - typically a
        # cycle so old its valid_times are all in the past.
        latest_cycle_iso = (gfs_cycle or hrrr_cycle).cycle_iso  # type: ignore[union-attr]
        raise RuntimeError(
            f"no forecast snapshots intersect race window "
            f"[{race_start.isoformat()}, {race_end.isoformat()}]; "
            f"latest cycle = {latest_cycle_iso}"
        )

    quality = "+".join(quality_parts) if len(quality_parts) > 1 else quality_parts[0]
    return WindForecast(snapshots=snapshots, quality=quality)


def _pick_bracketing(
    fhours: list[int],
    valid_times: list[datetime],
    t_start: datetime,
    t_end: datetime,
) -> list[int]:
    """Return fhours whose valid_times intersect [t_start, t_end], plus one
    bracketing fhour on each side so linear interpolation works at the edges.
    """
    in_window = [
        (fh, vt) for fh, vt in zip(fhours, valid_times)
        if t_start <= vt <= t_end
    ]
    picks: set[int] = {fh for fh, _ in in_window}

    # Add the latest fhour BEFORE t_start (so interpolation can sample at t_start).
    before = [(fh, vt) for fh, vt in zip(fhours, valid_times) if vt < t_start]
    if before:
        picks.add(max(before, key=lambda x: x[1])[0])

    # Add the earliest fhour AFTER t_end (so interpolation can sample at t_end).
    after = [(fh, vt) for fh, vt in zip(fhours, valid_times) if vt > t_end]
    if after:
        picks.add(min(after, key=lambda x: x[1])[0])

    return sorted(picks)
