"""NDBC observation provider — buoys + C-MAN shore stations.

Why NDBC
--------
One free, no-auth feed covers nearly every marine observation platform
in US waters: NOAA's own buoys, the GLOS/university Great Lakes buoys,
C-MAN shore stations, and partner platforms — ~1,300 stations across
the Great Lakes, all US coasts, Hawaii, and Alaska. Nothing about this
module is Great Lakes-specific; a race off San Francisco works the
same day it ships.

Two endpoints:

  * Station index (id, lat, lon, capabilities), refreshed by NDBC
    every ~5 min but effectively static for our purposes:
        https://www.ndbc.noaa.gov/activestations.xml
    Cached in Redis for 24 h (best-effort — if Redis is down we fetch
    direct, matching the app-wide non-fatal Redis pattern).

  * Observations, one fixed-width text file per station holding the
    last 45 DAYS of measurements at the station's native cadence
    (10 min for most buoys, hourly for some):
        https://www.ndbc.noaa.gov/data/realtime2/{ID}.txt
    The 45-day window is why the postprocess job needs no archive
    handling: it runs minutes after the finish, and even a --force
    rerun weeks later still finds the race window.

realtime2 format (newest row first)::

    #YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE
    #yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft
    2026 07 02 15 50 200  5.0  6.0   0.5     4   3.4 190 1015.2  22.1  20.3    MM   MM   MM    MM

``MM`` marks a missing measurement. Columns are resolved by NAME from
the header row, not by position — stations vary in which columns they
report, and NDBC has appended columns before.

Caveats encoded here:

  * Great Lakes buoys are seasonal (hauled out roughly Nov-Apr). A
    winter query simply finds no in-window obs — graceful None
    upstream, not an error.
  * Anemometer height is ~4-5 m on buoys vs the 10 m model reference.
    We store the RAW measured value; height correction is the
    reader's job (documented in base.py's schema notes).

Everything except ``NdbcProvider.fetch`` and ``_station_index`` is a
pure function — unit-tested against fixture text with zero I/O.
"""
from __future__ import annotations

import json
import logging
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

SOURCE = "ndbc"

STATION_INDEX_URL = "https://www.ndbc.noaa.gov/activestations.xml"
REALTIME2_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station_id}.txt"

# Redis cache for the station index. ~300 KB of XML parsed down to
# ~60 KB of JSON; refetching per postprocess run would be rude to NDBC
# and slow for us.
# v2: v1 cached lowercase lettered ids (the 404 bug) — key bumped so
# the fix doesn't wait out v1's 24 h TTL.
STATION_INDEX_CACHE_KEY = "obs:ndbc:station_index:v2"
STATION_INDEX_CACHE_TTL_S = 24 * 3600

# Station selection. 75 km keeps mid-lake buoys reachable from
# harbour-adjacent courses (Chicago -> Wilmette buoy 12 km, ->45007
# mid-lake ~90 km is deliberately excluded as too far to represent
# course conditions) while capping at 3 stations bounds the row size.
DEFAULT_MAX_STATIONS = 3
DEFAULT_MAX_DISTANCE_KM = 75.0

# Per-station row cap: a 30 h race at 10-min cadence is 180 rows; the
# cap only bites on pathological windows. Decimation keeps first/last.
MAX_OBS_PER_STATION = 400

HTTP_TIMEOUT_S = 20.0

# realtime2 column -> canonical snapshot key. Everything already in
# the unit we want (NDBC reports SI in realtime2). Columns not listed
# (VIS, PTDY, TIDE) are dropped — nobody debriefs a race on visibility
# in nautical miles.
_FIELD_MAP: dict[str, str] = {
    "WDIR": "wdir_deg",
    "WSPD": "wspd_mps",
    "GST": "gst_mps",
    "WVHT": "wvht_m",
    "DPD": "dpd_s",
    "APD": "apd_s",
    "MWD": "mwd_deg",
    "PRES": "pres_hpa",
    "ATMP": "atmp_c",
    "WTMP": "wtmp_c",
    "DEWP": "dewp_c",
}

_TIME_COLS = ("YY", "MM", "DD", "hh", "mm")


@dataclass(frozen=True)
class Station:
    station_id: str
    name: str
    lat: float
    lon: float


# ─── Pure functions ───────────────────────────────────────────────────


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. Good to ~0.5% — plenty for
    "which buoy is closest"."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_station_index(xml_text: str) -> list[Station]:
    """Parse activestations.xml into met-capable stations.

    Filters to ``met="y"`` — stations that report meteorological data.
    (DART tsunami buoys and water-quality-only platforms are useless
    for a race debrief.) Entries with malformed coordinates are
    skipped, not fatal: NDBC's index occasionally carries a station
    with an empty lat.
    """
    root = ET.fromstring(xml_text)
    stations: list[Station] = []
    for el in root.iter("station"):
        if el.get("met", "n") != "y":
            continue
        try:
            stations.append(
                Station(
                    # activestations.xml lists lettered station ids in
                    # LOWERCASE (chii2, oksi2) but the realtime2 file
                    # names are UPPERCASE (CHII2.txt) — lowercase URLs
                    # 404. Normalise here so the id is correct both in
                    # the fetch URL and in the stored snapshot.
                    # (Found 2026-07-02: every Chicago C-MAN station
                    # 404'd on the first backfill; numeric buoy ids
                    # were unaffected.)
                    station_id=el.attrib["id"].upper(),
                    name=el.get("name", el.attrib["id"]),
                    lat=float(el.attrib["lat"]),
                    lon=float(el.attrib["lon"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return stations


def nearest_stations(
    stations: list[Station],
    lat: float,
    lon: float,
    *,
    n: int = DEFAULT_MAX_STATIONS,
    max_km: float = DEFAULT_MAX_DISTANCE_KM,
) -> list[tuple[Station, float]]:
    """The ``n`` nearest stations within ``max_km``, closest first.

    Pure geometry — no region logic, which is exactly what makes this
    work anywhere NDBC has coverage.
    """
    scored = [
        (s, haversine_km(lat, lon, s.lat, s.lon)) for s in stations
    ]
    in_range = [(s, d) for s, d in scored if d <= max_km]
    in_range.sort(key=lambda sd: sd[1])
    return in_range[:n]


def _parse_value(token: str) -> Optional[float]:
    """One measurement token. ``MM`` (and any other junk) -> None."""
    if token == "MM":
        return None
    try:
        return float(token)
    except ValueError:
        return None


def parse_realtime2(text: str) -> list[dict]:
    """Parse a realtime2 file into obs dicts, OLDEST first.

    Each dict: ``{"ts": datetime(utc), <canonical field>: float|None}``.
    Only fields present in the file's header appear as keys. Rows with
    a malformed timestamp are skipped. Returns [] for an empty or
    headerless file (station exists in the index but publishes no
    realtime2 — happens for freshly deployed platforms).
    """
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#"):
        return []

    header = lines[0].lstrip("#").split()
    # Column index for each canonical field actually present.
    col_for: dict[str, int] = {}
    for i, name in enumerate(header):
        if name in _FIELD_MAP:
            col_for[_FIELD_MAP[name]] = i
    try:
        t_idx = [header.index(c) for c in _TIME_COLS]
    except ValueError:
        log.warning("ndbc: unrecognised realtime2 header: %r", lines[0])
        return []

    out: list[dict] = []
    for line in lines[1:]:
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        if len(tok) < len(header):
            # Short row — tolerate, but only fields we can index.
            pass
        try:
            yy, mo, dd, hh, mn = (int(tok[i]) for i in t_idx)
            ts = datetime(yy, mo, dd, hh, mn, tzinfo=timezone.utc)
        except (IndexError, ValueError):
            continue
        rec: dict = {"ts": ts}
        for field, i in col_for.items():
            rec[field] = _parse_value(tok[i]) if i < len(tok) else None
        out.append(rec)

    # File is newest-first; we store oldest-first so readers can plot
    # without re-sorting.
    out.sort(key=lambda r: r["ts"])
    return out


def filter_window(
    records: list[dict], t_start: datetime, t_end: datetime,
) -> list[dict]:
    """Obs with t_start <= ts <= t_end (inclusive both ends — a 10-min
    file exactly on the boundary is data, not noise)."""
    return [r for r in records if t_start <= r["ts"] <= t_end]


def decimate(records: list[dict], cap: int = MAX_OBS_PER_STATION) -> list[dict]:
    """Stride-sample down to ``cap`` rows, always keeping the last row
    (the finish-line conditions) — the JSONB row-size guard."""
    if len(records) <= cap:
        return records
    stride = math.ceil(len(records) / cap)
    kept = records[::stride]
    if kept[-1] is not records[-1]:
        kept.append(records[-1])
    return kept


def _serialise_obs(records: list[dict]) -> list[dict]:
    """datetime -> ISO string for JSONB storage."""
    out = []
    for r in records:
        d = dict(r)
        d["ts"] = r["ts"].isoformat()
        out.append(d)
    return out


# ─── The provider ─────────────────────────────────────────────────────


async def _get_text(client: httpx.AsyncClient, url: str) -> str:
    """One GET, raised on non-2xx. Module-level so tests monkeypatch a
    single seam instead of faking an httpx transport."""
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


class NdbcProvider:
    """ObservationProvider for the NDBC network. See module docstring."""

    source = SOURCE

    def __init__(
        self,
        *,
        max_stations: int = DEFAULT_MAX_STATIONS,
        max_km: float = DEFAULT_MAX_DISTANCE_KM,
    ) -> None:
        self.max_stations = max_stations
        self.max_km = max_km

    async def _station_index(self, client: httpx.AsyncClient) -> list[Station]:
        """Station index, Redis-cached 24 h, best-effort on the cache.

        Redis being down (or the key missing) degrades to a direct
        fetch; a direct-fetch failure propagates and is absorbed by
        ``base.build_obs_snapshot``'s provider isolation.
        """
        redis = None
        try:
            from app import redis_client
            redis = redis_client.get_client()
            raw = await redis.get(STATION_INDEX_CACHE_KEY)
            if raw:
                return [Station(**s) for s in json.loads(raw)]
        except Exception:  # noqa: BLE001 - cache is best-effort
            redis = None

        xml_text = await _get_text(client, STATION_INDEX_URL)
        stations = parse_station_index(xml_text)

        if redis is not None and stations:
            try:
                await redis.set(
                    STATION_INDEX_CACHE_KEY,
                    json.dumps([s.__dict__ for s in stations]),
                    ex=STATION_INDEX_CACHE_TTL_S,
                )
            except Exception:  # noqa: BLE001
                pass
        return stations

    async def fetch(
        self,
        *,
        lat: float,
        lon: float,
        t_start: datetime,
        t_end: datetime,
    ) -> list[dict]:
        """Station entries for the snapshot's ``stations[]`` array.

        Per-station failures (404 for a hauled-out buoy, timeout) skip
        that station and keep the rest — one dead buoy must not cost
        the debrief its other two.
        """
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            stations = await self._station_index(client)
            near = nearest_stations(
                stations, lat, lon, n=self.max_stations, max_km=self.max_km,
            )
            if not near:
                log.info(
                    "ndbc: no met stations within %.0f km of (%.3f, %.3f)",
                    self.max_km, lat, lon,
                )
                return []

            entries: list[dict] = []
            for station, dist_km in near:
                url = REALTIME2_URL.format(station_id=station.station_id)
                try:
                    text = await _get_text(client, url)
                except (httpx.HTTPError, OSError) as e:
                    log.info(
                        "ndbc: station %s realtime2 fetch failed (%s); skipping",
                        station.station_id, e,
                    )
                    continue
                obs = decimate(
                    filter_window(parse_realtime2(text), t_start, t_end)
                )
                if not obs:
                    log.info(
                        "ndbc: station %s has no obs in race window; skipping",
                        station.station_id,
                    )
                    continue
                entries.append(
                    {
                        "source": self.source,
                        "station_id": station.station_id,
                        "name": station.name,
                        "lat": station.lat,
                        "lon": station.lon,
                        "distance_km": round(dist_km, 1),
                        "obs_count": len(obs),
                        "obs": _serialise_obs(obs),
                    }
                )
            return entries
