"""Tests for app/routers/weather.py.

Mocks Redis at the module level (redis_client._client) and patches the GCS
fallback helper directly. No external services touched.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import redis_client
from app.routers import weather
from app.services.weather import ForecastNotAvailable


@pytest.fixture
def client():
    """Fresh app with only the weather router — skips the real lifespan."""
    app = FastAPI()
    app.include_router(weather.router)
    return TestClient(app)


@pytest.fixture
def fake_blob():
    """Gzipped JSON shaped like the worker's real output."""
    payload = {
        "source": "hrrr",
        "reference_time": "2026-04-29T16:00:00+00:00",
        "valid_time": "2026-04-29T17:00:00+00:00",
        "bbox": {"min_lat": 24.0, "max_lat": 50.0, "min_lon": -126.0, "max_lon": -66.0},
        "shape": [2, 2],
        "lats": [24.0, 50.0],
        "lons": [-126.0, -66.0],
        "u": [[1.0, 2.0], [3.0, 4.0]],
        "v": [[0.5, 0.6], [0.7, 0.8]],
    }
    return gzip.compress(json.dumps(payload).encode())


@pytest.fixture
def expected_etag(fake_blob):
    return f'"{hashlib.sha256(fake_blob).hexdigest()[:16]}"'


@pytest.fixture
def mock_redis(monkeypatch):
    """Replace the module-level Redis client with an AsyncMock."""
    mock = AsyncMock()
    monkeypatch.setattr(redis_client, "_client", mock)
    monkeypatch.setattr(redis_client, "_startup_error", None)
    return mock


# -- 200 happy path -------------------------------------------------------

def test_returns_gzipped_payload_with_headers(client, mock_redis, fake_blob, expected_etag):
    mock_redis.get.return_value = fake_blob

    r = client.get("/api/weather?region=conus&source=hrrr")

    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["etag"] == expected_etag
    assert r.headers["cache-control"] == "public, max-age=300"
    assert r.headers["vary"] == "Accept-Encoding"
    assert r.content == gzip.decompress(fake_blob)
    mock_redis.get.assert_awaited_once_with("weather:hrrr:conus:latest")


def test_conus_gfs_uses_region_scoped_key(client, mock_redis, fake_blob):
    mock_redis.get.return_value = fake_blob

    r = client.get("/api/weather?region=conus&source=gfs")

    assert r.status_code == 200
    mock_redis.get.assert_awaited_once_with("weather:gfs:conus:latest")


def test_venue_uses_region_scoped_key(client, mock_redis, fake_blob):
    """Venue regions follow the same key shape as base regions."""
    mock_redis.get.return_value = fake_blob

    r = client.get("/api/weather?region=sf_bay&source=hrrr")

    assert r.status_code == 200
    mock_redis.get.assert_awaited_once_with("weather:hrrr:sf_bay:latest")


def test_hawaii_uses_region_scoped_gfs_key(client, mock_redis, fake_blob):
    mock_redis.get.return_value = fake_blob

    r = client.get("/api/weather?region=hawaii&source=gfs")

    assert r.status_code == 200
    mock_redis.get.assert_awaited_once_with("weather:gfs:hawaii:latest")


# -- ETag / 304 -----------------------------------------------------------

def test_if_none_match_match_returns_304(client, mock_redis, fake_blob, expected_etag):
    mock_redis.get.return_value = fake_blob

    r = client.get(
        "/api/weather?region=conus&source=hrrr",
        headers={"If-None-Match": expected_etag},
    )

    assert r.status_code == 304
    assert r.headers["etag"] == expected_etag
    assert r.content == b""
    assert "content-encoding" not in r.headers


def test_if_none_match_mismatch_returns_200(client, mock_redis, fake_blob):
    mock_redis.get.return_value = fake_blob

    r = client.get(
        "/api/weather?region=conus&source=hrrr",
        headers={"If-None-Match": '"deadbeefdeadbeef"'},
    )

    assert r.status_code == 200
    assert r.content == gzip.decompress(fake_blob)


# -- Validation -----------------------------------------------------------

def test_unknown_region_returns_404(client, mock_redis):
    r = client.get("/api/weather?region=atlantic&source=hrrr")

    assert r.status_code == 404
    mock_redis.get.assert_not_awaited()


def test_unknown_source_returns_400(client, mock_redis):
    r = client.get("/api/weather?region=conus&source=ecmwf")

    assert r.status_code == 400
    mock_redis.get.assert_not_awaited()


def test_hrrr_on_hawaii_returns_400(client, mock_redis):
    """Hawaii is GFS-only — HRRR doesn't cover it."""
    r = client.get("/api/weather?region=hawaii&source=hrrr")

    assert r.status_code == 400
    mock_redis.get.assert_not_awaited()


def test_gfs_on_venue_returns_400(client, mock_redis):
    """Venues are HRRR-only — GFS native is too coarse for buoy racing."""
    r = client.get("/api/weather?region=sf_bay&source=gfs")

    assert r.status_code == 400
    mock_redis.get.assert_not_awaited()


# -- GCS fallback ---------------------------------------------------------

def test_gcs_fallback_when_redis_empty(client, mock_redis, fake_blob, monkeypatch):
    mock_redis.get.return_value = None
    captured = {}

    def fake_gcs(source, region):
        captured["args"] = (source, region)
        return fake_blob

    monkeypatch.setattr(weather, "_read_latest_gcs", fake_gcs)

    r = client.get("/api/weather?region=sf_bay&source=hrrr")

    assert r.status_code == 200
    assert r.content == gzip.decompress(fake_blob)
    assert captured["args"] == ("hrrr", "sf_bay")


def test_503_when_redis_and_gcs_both_empty(client, mock_redis, monkeypatch):
    mock_redis.get.return_value = None
    monkeypatch.setattr(weather, "_read_latest_gcs", lambda src, region: None)

    r = client.get("/api/weather?region=conus&source=hrrr")

    assert r.status_code == 503


# -- Time-sliced read (`at`) ----------------------------------------------
#
# These patch the loader (load_grid_blob_at) to keep the router tests
# focused on HTTP wiring. The nearest-fhour selection itself is covered in
# test_forecast_loader.py against a real fake-Redis store.


def test_at_returns_nearest_grid_with_headers(client, fake_blob, expected_etag, monkeypatch):
    chosen = datetime(2026, 4, 29, 19, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        weather, "load_grid_blob_at", AsyncMock(return_value=(fake_blob, chosen))
    )

    r = client.get("/api/weather?region=conus&source=hrrr&at=2026-04-29T19:05:00Z")

    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["etag"] == expected_etag
    assert r.headers["cache-control"] == "public, max-age=300"
    assert r.headers["vary"] == "Accept-Encoding"
    assert r.content == gzip.decompress(fake_blob)
    weather.load_grid_blob_at.assert_awaited_once()
    args = weather.load_grid_blob_at.await_args.args
    assert args[0] == "hrrr"
    assert args[1] == "conus"
    assert args[2] == datetime(2026, 4, 29, 19, 5, tzinfo=timezone.utc)


def test_at_if_none_match_returns_304(client, fake_blob, expected_etag, monkeypatch):
    chosen = datetime(2026, 4, 29, 19, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        weather, "load_grid_blob_at", AsyncMock(return_value=(fake_blob, chosen))
    )

    r = client.get(
        "/api/weather?region=conus&source=hrrr&at=2026-04-29T19:05:00Z",
        headers={"If-None-Match": expected_etag},
    )

    assert r.status_code == 304
    assert r.headers["etag"] == expected_etag
    assert r.content == b""
    assert "content-encoding" not in r.headers


def test_at_invalid_timestamp_returns_400(client, monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(weather, "load_grid_blob_at", spy)

    r = client.get("/api/weather?region=conus&source=hrrr&at=not-a-date")

    assert r.status_code == 400
    spy.assert_not_awaited()


def test_at_past_horizon_returns_425(client, monkeypatch):
    available_at = datetime(2026, 4, 29, 18, 0, tzinfo=timezone.utc)

    async def _raise(*_args, **_kwargs):
        raise ForecastNotAvailable(available_at=available_at, reason="past horizon")

    monkeypatch.setattr(weather, "load_grid_blob_at", _raise)

    r = client.get("/api/weather?region=conus&source=hrrr&at=2026-05-10T12:00:00Z")

    assert r.status_code == 425
    body = r.json()["detail"]
    assert body["available_at"] == available_at.isoformat()
    assert "hours_until_available" in body


def test_at_missing_snapshot_returns_503(client, monkeypatch):
    async def _raise(*_args, **_kwargs):
        raise RuntimeError("missing forecast snapshot")

    monkeypatch.setattr(weather, "load_grid_blob_at", _raise)

    r = client.get("/api/weather?region=conus&source=hrrr&at=2026-04-29T19:00:00Z")

    assert r.status_code == 503
