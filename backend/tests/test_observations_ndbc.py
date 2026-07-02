"""Tests for app/services/observations — NDBC provider + snapshot glue.

Layout mirrors the module: pure-function unit tests against inline
fixture text (no I/O), then mocked-fetch orchestration tests for
``NdbcProvider.fetch`` and ``build_obs_snapshot``, then a real-NDBC
smoke test gated behind RUN_REAL_NOAA_TESTS=1 (same gate as
test_weather_ingest_live.py).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services.observations import build_obs_snapshot
from app.services.observations import ndbc
from app.services.observations.ndbc import (
    NdbcProvider,
    Station,
    decimate,
    filter_window,
    haversine_km,
    nearest_stations,
    parse_realtime2,
    parse_station_index,
)


T0 = datetime(2026, 7, 2, 17, 0, tzinfo=timezone.utc)


# ─── Fixture text ─────────────────────────────────────────────────────

# Trimmed activestations.xml: one Lake Michigan met buoy, one met
# C-MAN station, one met="n" water-quality platform (must be dropped),
# one with a broken lat (must be skipped, not fatal).
STATIONS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<stations created="2026-07-02T16:50:00UTC" count="4">
  <station id="45007" lat="42.674" lon="-87.026" name="SOUTH MICHIGAN"
           owner="NDBC" pgm="NDBC Meteorological/Ocean" type="buoy"
           met="y" currents="n" waterquality="n" dart="n"/>
  <station id="CHII2" lat="41.916" lon="-87.572" name="Chicago, IL"
           owner="NDBC" pgm="C-MAN" type="fixed"
           met="y" currents="n" waterquality="n" dart="n"/>
  <station id="45999" lat="42.0" lon="-87.5" name="WQ ONLY"
           owner="X" pgm="IOOS" type="buoy"
           met="n" currents="n" waterquality="y" dart="n"/>
  <station id="45998" lat="" lon="-87.5" name="BROKEN"
           owner="X" pgm="IOOS" type="buoy"
           met="y" currents="n" waterquality="n" dart="n"/>
</stations>
"""

# realtime2 excerpt — newest first, MM = missing, WTMP/DEWP partly
# missing, rows spanning 17:00..17:30 UTC on 2026-07-02.
REALTIME2_TXT = """\
#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft
2026 07 02 17 30 210  6.2  7.8   0.7     4   3.6 200 1014.8  22.5    MM    MM   MM   MM    MM
2026 07 02 17 20 205  5.5  7.0   0.6     4   3.5 195 1015.0  22.3  20.4  18.1   MM   MM    MM
2026 07 02 17 10  MM   MM   MM   0.5     4   3.4 190 1015.1  22.2  20.3  18.0   MM   MM    MM
2026 07 02 17 00 200  5.0  6.5   0.5     4   3.4 190 1015.2  22.1  20.3  18.0   MM   MM    MM
2026 07 02 16 50 198  4.8  6.0   0.5     4   3.3 188 1015.3  22.0  20.2  17.9   MM   MM    MM
"""


# ─── Pure functions ───────────────────────────────────────────────────


def test_haversine_chicago_to_milwaukee():
    # Chicago Loop -> Milwaukee harbor, ~131 km.
    d = haversine_km(41.882, -87.628, 43.039, -87.895)
    assert 125 < d < 137


def test_parse_station_index_filters_and_survives_junk():
    stations = parse_station_index(STATIONS_XML)
    ids = {s.station_id for s in stations}
    assert ids == {"45007", "CHII2"}          # met-only, broken row dropped
    s45007 = next(s for s in stations if s.station_id == "45007")
    assert s45007.lat == pytest.approx(42.674)
    assert s45007.name == "SOUTH MICHIGAN"


def test_nearest_stations_orders_and_cuts_by_distance():
    stations = parse_station_index(STATIONS_XML)
    # Query point just off Chicago: CHII2 is close, 45007 is ~90 km NE.
    near = nearest_stations(stations, 41.95, -87.60, n=3, max_km=75.0)
    assert [s.station_id for s, _ in near] == ["CHII2"]
    # Widen the radius and both appear, closest first.
    near = nearest_stations(stations, 41.95, -87.60, n=3, max_km=150.0)
    assert [s.station_id for s, _ in near] == ["CHII2", "45007"]
    dists = [d for _, d in near]
    assert dists == sorted(dists)


def test_nearest_stations_respects_n_cap():
    stations = parse_station_index(STATIONS_XML)
    near = nearest_stations(stations, 41.95, -87.60, n=1, max_km=500.0)
    assert len(near) == 1
    assert near[0][0].station_id == "CHII2"


def test_parse_realtime2_values_order_and_missing():
    recs = parse_realtime2(REALTIME2_TXT)
    assert len(recs) == 5
    # Oldest first.
    assert recs[0]["ts"] == datetime(2026, 7, 2, 16, 50, tzinfo=timezone.utc)
    assert recs[-1]["ts"] == datetime(2026, 7, 2, 17, 30, tzinfo=timezone.utc)
    # Numeric conversion.
    r1700 = next(r for r in recs if r["ts"] == T0)
    assert r1700["wdir_deg"] == 200.0
    assert r1700["wspd_mps"] == 5.0
    assert r1700["wvht_m"] == 0.5
    assert r1700["pres_hpa"] == 1015.2
    # MM -> None (calm-sensor dropout row).
    r1710 = next(
        r for r in recs
        if r["ts"] == datetime(2026, 7, 2, 17, 10, tzinfo=timezone.utc)
    )
    assert r1710["wspd_mps"] is None
    assert r1710["wvht_m"] == 0.5
    # VIS/PTDY/TIDE never become keys.
    assert "vis" not in {k.lower()[:3] for k in r1700}


def test_parse_realtime2_empty_and_headerless():
    assert parse_realtime2("") == []
    assert parse_realtime2("2026 07 02 17 00 200 5.0\n") == []


def test_filter_window_inclusive_bounds():
    recs = parse_realtime2(REALTIME2_TXT)
    got = filter_window(recs, T0, T0 + timedelta(minutes=20))
    assert [r["ts"].minute for r in got] == [0, 10, 20]


def test_decimate_caps_and_keeps_last():
    recs = [{"ts": T0 + timedelta(minutes=i)} for i in range(1000)]
    out = decimate(recs, cap=100)
    assert len(recs) == 1000                   # input untouched
    assert len(out) <= 101                     # cap + possible last append
    assert out[0] is recs[0]
    assert out[-1] is recs[-1]                 # finish conditions kept
    assert decimate(recs[:50], cap=100) == recs[:50]   # under cap: no-op


# ─── Provider orchestration (mocked I/O) ──────────────────────────────


@pytest.fixture
def mock_ndbc_http(monkeypatch: pytest.MonkeyPatch):
    """Route _get_text by URL; bypass Redis (index fetched direct)."""
    fetched: list[str] = []

    async def fake_get_text(client, url: str) -> str:
        fetched.append(url)
        if url == ndbc.STATION_INDEX_URL:
            return STATIONS_XML
        if url.endswith("CHII2.txt"):
            return REALTIME2_TXT
        if url.endswith("45007.txt"):
            raise httpx.HTTPError("simulated 404")
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(ndbc, "_get_text", fake_get_text)
    return fetched


async def test_provider_fetch_happy_path(mock_ndbc_http):
    provider = NdbcProvider(max_stations=3, max_km=150.0)
    entries = await provider.fetch(
        lat=41.95, lon=-87.60,
        t_start=T0, t_end=T0 + timedelta(minutes=30),
    )
    # CHII2 delivered; 45007's simulated fetch failure was skipped.
    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "ndbc"
    assert e["station_id"] == "CHII2"
    assert e["distance_km"] > 0
    assert e["obs_count"] == 4                 # 17:00..17:30 inclusive
    assert e["obs"][0]["ts"] == T0.isoformat() # serialised, oldest first
    assert e["obs"][0]["wspd_mps"] == 5.0


async def test_provider_fetch_no_stations_in_range(mock_ndbc_http):
    provider = NdbcProvider(max_km=5.0)
    entries = await provider.fetch(
        lat=21.3, lon=-157.9,                  # Honolulu — nothing in fixture
        t_start=T0, t_end=T0 + timedelta(hours=1),
    )
    assert entries == []


async def test_provider_fetch_no_obs_in_window(mock_ndbc_http):
    provider = NdbcProvider(max_km=150.0)
    entries = await provider.fetch(
        lat=41.95, lon=-87.60,
        t_start=T0 + timedelta(days=10),       # outside fixture data
        t_end=T0 + timedelta(days=10, hours=3),
    )
    assert entries == []


# ─── Snapshot envelope ────────────────────────────────────────────────


class _FakeProvider:
    source = "fake"

    def __init__(self, entries=None, error: bool = False):
        self._entries = entries or []
        self._error = error

    async def fetch(self, *, lat, lon, t_start, t_end):
        if self._error:
            raise RuntimeError("provider exploded")
        return self._entries


async def test_build_obs_snapshot_envelope():
    entry = {"source": "fake", "station_id": "X", "obs": [], "obs_count": 0}
    snap = await build_obs_snapshot(
        lat=42.0, lon=-87.6,
        t_start=T0, t_end=T0 + timedelta(hours=3),
        providers=[_FakeProvider([entry])],
    )
    assert snap is not None
    assert snap["schema_version"] == 1
    assert snap["center"] == [42.0, -87.6]
    assert snap["t_start"] == T0.isoformat()
    assert snap["stations"] == [entry]


async def test_build_obs_snapshot_none_when_no_data():
    snap = await build_obs_snapshot(
        lat=42.0, lon=-87.6,
        t_start=T0, t_end=T0 + timedelta(hours=3),
        providers=[_FakeProvider([])],
    )
    assert snap is None


async def test_build_obs_snapshot_provider_failure_isolated():
    """A dead provider must not cost us the healthy one's data."""
    entry = {"source": "fake", "station_id": "X", "obs": [], "obs_count": 0}
    snap = await build_obs_snapshot(
        lat=42.0, lon=-87.6,
        t_start=T0, t_end=T0 + timedelta(hours=3),
        providers=[_FakeProvider(error=True), _FakeProvider([entry])],
    )
    assert snap is not None
    assert len(snap["stations"]) == 1


async def test_build_obs_snapshot_rejects_inverted_window():
    with pytest.raises(ValueError):
        await build_obs_snapshot(
            lat=42.0, lon=-87.6,
            t_start=T0, t_end=T0 - timedelta(hours=1),
            providers=[_FakeProvider([])],
        )


# ─── Live smoke (gated) ───────────────────────────────────────────────


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("RUN_REAL_NOAA_TESTS"),
    reason="set RUN_REAL_NOAA_TESTS=1 to run real-NDBC smoke tests",
)
async def test_real_ndbc_off_chicago():
    """Hit real NDBC: index + realtime2 for the nearest stations to a
    point off Chicago over the last 6 h. Catches URL-scheme or format
    drift. Seasonal note: Great Lakes buoys are hauled Nov-Apr — if
    this fails in winter with zero stations, check a coastal point
    (e.g. 33.7, -118.2 off Long Beach) before blaming the code."""
    now = datetime.now(timezone.utc)
    snap = await build_obs_snapshot(
        lat=41.95, lon=-87.60,
        t_start=now - timedelta(hours=6), t_end=now,
    )
    assert snap is not None, "no NDBC data near Chicago in the last 6 h"
    assert snap["stations"], "empty stations array"
    st = snap["stations"][0]
    assert st["source"] == "ndbc"
    assert st["obs_count"] > 0
    # At least one observation carries a real wind or wave measurement.
    assert any(
        o.get("wspd_mps") is not None or o.get("wvht_m") is not None
        for st in snap["stations"] for o in st["obs"]
    )
