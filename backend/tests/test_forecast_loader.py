# backend/tests/test_forecast_loader.py
"""Tests for forecast_loader — cycle selection + WindForecast assembly.

Mocks Redis (zrevrange + get) at the module boundary. Covers four key
paths:
    1. Race within HRRR window, short race → HRRR-only forecast
    2. Race within HRRR window, long race → HRRR + GFS hybrid
    3. Race past HRRR horizon → ForecastNotAvailable
    4. No ingested cycles → RuntimeError
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services.weather.forecast_loader import (
    ForecastNotAvailable,
    load_forecast_for_race,
)


# ─── Helpers ─────────────────────────────────────────────────────────────


def _wind_blob(valid_iso: str, source: str = "hrrr") -> bytes:
    payload = {
        "source": source,
        "reference_time": "2026-05-05T12:00:00+00:00",
        "valid_time": valid_iso,
        "lats": [41.0, 42.0, 43.0],
        "lons": [-89.0, -88.0, -87.0],
        "u": [[1.0] * 3] * 3,
        "v": [[2.0] * 3] * 3,
    }
    return gzip.compress(json.dumps(payload).encode())


def _make_manifest(source: str, fhours: list[int], cycle_iso: str = "20260505T1200Z") -> dict:
    ref = datetime(2026, 5, 5, 12, tzinfo=timezone.utc)
    return {
        "source": source,
        "region": "conus",
        "cycle": cycle_iso,
        "reference_time": ref.isoformat(),
        "fhours": fhours,
        "valid_times": [(ref + timedelta(hours=h)).isoformat() for h in fhours],
    }


class _FakeRedis:
    """Minimal AsyncMock-ish redis stand-in keyed by the actual lookup pattern.

    Driving the loader through real key strings rather than ordered side_effects
    keeps the tests robust to internal lookup-order changes.
    """
    def __init__(self, store: dict[str, bytes]):
        self.store = store

    async def get(self, key):
        return self.store.get(key)

    async def zrevrange(self, key, start, stop):
        # Stored under the same key as bytes-list of cycle ISOs (newest first)
        return self.store.get(key, [])


@pytest.fixture
def freeze_now(monkeypatch):
    """Pin datetime.now used inside forecast_loader."""
    fake_now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now if tz else fake_now.replace(tzinfo=None)

    monkeypatch.setattr(
        "app.services.weather.forecast_loader.datetime", _DT,
    )
    return fake_now


# ─── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_short_race_inside_hrrr_window_returns_hrrr_only(freeze_now):
    fhours = list(range(0, 19))
    manifest = _make_manifest("hrrr", fhours)
    cycle_iso = manifest["cycle"]

    store: dict = {
        "weather:hrrr:conus:cycles": [cycle_iso.encode()],
        f"weather:hrrr:conus:{cycle_iso}:manifest": json.dumps(manifest).encode(),
    }
    for fh in fhours:
        valid = (freeze_now + timedelta(hours=fh)).isoformat()
        store[f"weather:hrrr:conus:{cycle_iso}:f{fh:03d}"] = _wind_blob(valid, "hrrr")

    fake = _FakeRedis(store)
    with patch("app.services.weather.forecast_loader.redis_client.get_client",
               return_value=fake):
        race_start = freeze_now + timedelta(hours=2)
        forecast = await load_forecast_for_race("conus", race_start, duration_hours=4)

    assert forecast.quality == "hrrr"
    # Should pick fhours covering [race_start-bracket, race_start+4h+bracket]
    assert all(s.source == "hrrr" for s in forecast.snapshots)
    assert len(forecast.snapshots) >= 4  # 4h race + bracketing fhours


@pytest.mark.asyncio
async def test_long_race_uses_hrrr_plus_gfs_hybrid(freeze_now):
    """Race extending past HRRR horizon pulls GFS for the tail."""
    hrrr_fhours = list(range(0, 19))
    gfs_fhours = list(range(0, 121, 3))
    hrrr_manifest = _make_manifest("hrrr", hrrr_fhours)
    gfs_manifest = _make_manifest("gfs", gfs_fhours)
    cycle = hrrr_manifest["cycle"]

    store: dict = {
        "weather:hrrr:conus:cycles": [cycle.encode()],
        "weather:gfs:conus:cycles": [cycle.encode()],
        f"weather:hrrr:conus:{cycle}:manifest": json.dumps(hrrr_manifest).encode(),
        f"weather:gfs:conus:{cycle}:manifest": json.dumps(gfs_manifest).encode(),
    }
    for fh in hrrr_fhours:
        valid = (freeze_now + timedelta(hours=fh)).isoformat()
        store[f"weather:hrrr:conus:{cycle}:f{fh:03d}"] = _wind_blob(valid, "hrrr")
    for fh in gfs_fhours:
        valid = (freeze_now + timedelta(hours=fh)).isoformat()
        store[f"weather:gfs:conus:{cycle}:f{fh:03d}"] = _wind_blob(valid, "gfs")

    fake = _FakeRedis(store)
    with patch("app.services.weather.forecast_loader.redis_client.get_client",
               return_value=fake):
        # Race starts at +1h, lasts 30h → spans HRRR (1-18h) + GFS (18-31h)
        race_start = freeze_now + timedelta(hours=1)
        forecast = await load_forecast_for_race("conus", race_start, duration_hours=30)

    sources = {s.source for s in forecast.snapshots}
    assert sources == {"hrrr", "gfs"}
    assert forecast.quality == "hrrr+gfs"


@pytest.mark.asyncio
async def test_race_past_hrrr_horizon_raises_forecast_not_available(freeze_now):
    fake = _FakeRedis(store={})  # store irrelevant — should fail before lookup
    with patch("app.services.weather.forecast_loader.redis_client.get_client",
               return_value=fake):
        race_start = freeze_now + timedelta(hours=24)  # 24 > 18h horizon
        with pytest.raises(ForecastNotAvailable) as exc:
            await load_forecast_for_race("conus", race_start, duration_hours=4)

    # available_at should be race_start - 18h; hours_until_available > 0
    assert exc.value.hours_until_available > 0
    assert exc.value.available_at < race_start


@pytest.mark.asyncio
async def test_no_ingested_cycles_raises_runtime_error(freeze_now):
    """HRRR window passes the check, but cycles index is empty → operational error."""
    fake = _FakeRedis(store={
        # zrevrange on these keys returns [] because they don't exist
    })
    with patch("app.services.weather.forecast_loader.redis_client.get_client",
               return_value=fake):
        race_start = freeze_now + timedelta(hours=2)
        with pytest.raises(RuntimeError, match="no ingested cycles"):
            await load_forecast_for_race("conus", race_start, duration_hours=4)


@pytest.mark.asyncio
async def test_unknown_region_raises_value_error(freeze_now):
    fake = _FakeRedis(store={})
    with patch("app.services.weather.forecast_loader.redis_client.get_client",
               return_value=fake):
        with pytest.raises(ValueError, match="unknown region"):
            await load_forecast_for_race(
                "atlantis", freeze_now + timedelta(hours=1), duration_hours=4,
            )


@pytest.mark.asyncio
async def test_naive_race_start_assumed_utc(freeze_now):
    """User submits race_start without tzinfo → loader assumes UTC."""
    fhours = list(range(0, 19))
    manifest = _make_manifest("hrrr", fhours)
    cycle = manifest["cycle"]
    store: dict = {
        "weather:hrrr:conus:cycles": [cycle.encode()],
        f"weather:hrrr:conus:{cycle}:manifest": json.dumps(manifest).encode(),
    }
    for fh in fhours:
        valid = (freeze_now + timedelta(hours=fh)).isoformat()
        store[f"weather:hrrr:conus:{cycle}:f{fh:03d}"] = _wind_blob(valid)

    fake = _FakeRedis(store)
    naive_start = (freeze_now + timedelta(hours=2)).replace(tzinfo=None)
    with patch("app.services.weather.forecast_loader.redis_client.get_client",
               return_value=fake):
        forecast = await load_forecast_for_race("conus", naive_start, duration_hours=4)
    assert forecast.quality == "hrrr"