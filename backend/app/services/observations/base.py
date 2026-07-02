"""Provider protocol + snapshot envelope for observed conditions.

What we store
-------------
``race_sessions.obs_snapshot`` (JSONB, migration 0023) holds the
observed conditions near the racecourse over the race window::

    {
        "schema_version": 1,
        "generated_at": "2026-07-02T21:14:00Z",
        "t_start":      "2026-07-02T17:45:00Z",
        "t_end":        "2026-07-02T21:15:00Z",
        "center":       [42.05, -87.74],       # query point (lat, lon)
        "stations": [
            {
                "source":      "ndbc",
                "station_id":  "45174",
                "name":        "Wilmette Buoy",
                "lat":         42.135, "lon": -87.655,
                "distance_km": 12.4,
                "obs_count":   21,
                "obs": [
                    {"ts": "2026-07-02T17:50:00Z", "wdir_deg": 200.0,
                     "wspd_mps": 5.0, "gst_mps": 6.5, "wvht_m": 0.5,
                     "dpd_s": 4.0, "apd_s": 3.4, "mwd_deg": 190.0,
                     "pres_hpa": 1015.2, "atmp_c": 22.1, "wtmp_c": 20.3,
                     "dewp_c": null},
                    ...
                ]
            },
            ...
        ]
    }

Design notes:

  * Every station entry is tagged with ``source`` so a future CO-OPS
    (tides/currents) or international provider slots in without a
    schema change — readers filter/group by source. Hardcoding NDBC
    assumptions into the schema was the technical-debt path we
    explicitly avoided.
  * Missing measurements are ``null``, mirroring the wind_snapshot
    convention ("no data here", never zero).
  * Wind speeds are stored in m/s — the same unit wind_snapshot uses —
    so forecast-vs-actual comparison needs no conversion. NOTE for
    readers doing that comparison: buoy anemometers sit at ~4-5 m vs
    the model's 10 m reference; apply a ~1.1x height correction to the
    buoy wind before calling the forecast wrong.
  * Size budget: 3 stations x 10-min cadence x 3.5 h race ~= 60 obs x
    ~11 fields ~= 25 KB JSON. A 30 h Mac race is bounded by the
    per-station decimation cap in the provider (see
    ``ndbc.MAX_OBS_PER_STATION``), worst case ~150 KB — same order as
    a Mac-race wind_snapshot.

This module is orchestration-only glue: providers do the I/O, and a
provider failure never propagates (a dead NOAA endpoint must not fail
the postprocess job — mirrors the wind-snapshot "returning None is not
an error" philosophy).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@runtime_checkable
class ObservationProvider(Protocol):
    """One network of observation stations (NDBC, CO-OPS, ...).

    ``fetch`` returns a list of station entries — dicts matching the
    ``stations[]`` element of the snapshot schema above (the provider
    fills ``source``). An empty list means "no usable stations near
    this point / no data in this window" and is not an error.
    """

    source: str

    async def fetch(
        self,
        *,
        lat: float,
        lon: float,
        t_start: datetime,
        t_end: datetime,
    ) -> list[dict]: ...


def default_providers() -> list[ObservationProvider]:
    """The providers a standard postprocess run queries.

    Late import so ``base`` never depends on a concrete provider
    module (keeps the dependency arrow pointing one way).
    """
    from app.services.observations.ndbc import NdbcProvider

    return [NdbcProvider()]


async def build_obs_snapshot(
    *,
    lat: float,
    lon: float,
    t_start: datetime,
    t_end: datetime,
    providers: Optional[list[ObservationProvider]] = None,
) -> Optional[dict]:
    """Query every provider and assemble the snapshot envelope.

    Returns None when no provider produced any station data — the
    column stays null and the next postprocess run retries cheaply
    (same backfill semantics as heel_summary).

    Per-provider exceptions are swallowed and logged: one flaky
    network must never cost us the data from the others, and nothing
    here may fail the postprocess job.
    """
    if t_end <= t_start:
        raise ValueError("t_end must be strictly after t_start")

    provs = providers if providers is not None else default_providers()

    stations: list[dict] = []
    for p in provs:
        try:
            entries = await p.fetch(
                lat=lat, lon=lon, t_start=t_start, t_end=t_end,
            )
        except Exception as e:  # noqa: BLE001 - provider isolation
            log.warning(
                "obs snapshot: provider %r failed (%s); continuing",
                getattr(p, "source", type(p).__name__), e,
            )
            continue
        stations.extend(entries)

    if not stations:
        log.info(
            "obs snapshot: no station data near (%.3f, %.3f) for %s..%s",
            lat, lon, t_start.isoformat(), t_end.isoformat(),
        )
        return None

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "t_start": t_start.isoformat(),
        "t_end": t_end.isoformat(),
        "center": [lat, lon],
        "stations": stations,
    }
