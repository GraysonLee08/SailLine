"""Tests for app/routers/weather.py.

Mocks Redis at the module level (redis_client._client) and patches the GCS
fallback helper directly. No external services touched.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import redis_client
from app.routers import weather


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
        "bbox": {"min_lat": 40.0, "max_lat": 50.0, "min_lon": -94.0, "max_lon": -75.0},
        "shape": [2, 2],
        "lats": [40.0, 50.0],
        "lons": [-94.0, -75.0],
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

    r = client.get("/api/weather?region=great_lakes&source=hrrr")

    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["etag"] == expected_etag
    assert r.headers["cache-control"] == "public, max-age=300"
    assert r.headers["vary"] == "Accept-Encoding"
    assert r.content == gzip.decompress(fake_blob)
    mock_redis.get.assert_awaited_once_with("weather:hrrr:latest")


def test_gfs_source_uses_gfs_key(client, mock_redis, fake_blob):
    mock_redis.get.return_value = fake_blob

    r = client.get("/api/weather?region=great_lakes&source=gfs")

    assert r.status_code == 200
    mock_redis.get.assert_awaited_once_with("weather:gfs:latest")


# -- ETag / 304 -----------------------------------------------------------

def test_if_none_match_match_returns_304(client, mock_redis, fake_blob, expected_etag):
    mock_redis.get.return_value = fake_blob

    r = client.get(
        "/api/weather?region=great_lakes&source=hrrr",
        headers={"If-None-Match": expected_etag},
    )

    assert r.status_code == 304
    assert r.headers["etag"] == expected_etag
    assert r.content == b""
    assert "content-encoding" not in r.headers


def test_if_none_match_mismatch_returns_200(client, mock_redis, fake_blob):
    mock_redis.get.return_value = fake_blob

    r = client.get(
        "/api/weather?region=great_lakes&source=hrrr",
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
    r = client.get("/api/weather?region=great_lakes&source=ecmwf")

    assert r.status_code == 400
    mock_redis.get.assert_not_awaited()


# -- Fallback -------------------------------------------------------------

def test_gcs_fallback_when_redis_empty(client, mock_redis, fake_blob, monkeypatch):
    mock_redis.get.return_value = None
    monkeypatch.setattr(weather, "_read_latest_gcs", lambda source: fake_blob)

    r = client.get("/api/weather?region=great_lakes&source=hrrr")

    assert r.status_code == 200
    assert r.content == gzip.decompress(fake_blob)


def test_503_when_redis_and_gcs_both_empty(client, mock_redis, monkeypatch):
    mock_redis.get.return_value = None
    monkeypatch.setattr(weather, "_read_latest_gcs", lambda source: None)

    r = client.get("/api/weather?region=great_lakes&source=hrrr")

    assert r.status_code == 503