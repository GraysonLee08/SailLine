"""Tests for workers/weather_ingest.py.

External services (NOAA, Redis, GCS) and time are all mocked. Run with:
    python -m pytest tests/test_weather_ingest.py -v

For a real-NOAA smoke test, see tests/test_weather_ingest_live.py.
"""
from __future__ import annotations

import gzip
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.services.grib import WindGrid
from workers import weather_ingest
from workers.weather_ingest import (
    SOURCES,
    WIND_FIELDS,
    clip_and_serialize,
    fetch_ranges,
    gfs_url,
    hrrr_url,
    ingest,
    latest_cycle,
)


# ---------------------------------------------------------------------------
# Helpers


def _synthetic_grid() -> WindGrid:
    """3x3 wind grid inside the great_lakes bbox, with known reference time."""
    return WindGrid(
        lats=np.array([41.0, 42.0, 43.0]),
        lons=np.array([-90.0, -89.0, -88.0]),
        u=np.full((3, 3), 5.0, dtype=np.float32),
        v=np.full((3, 3), -3.0, dtype=np.float32),
        reference_time=datetime(2026, 4, 29, 6, tzinfo=timezone.utc),
        valid_time=datetime(2026, 4, 29, 12, tzinfo=timezone.utc),
        source="gfs",
    )


def _mock_resp(body: bytes) -> MagicMock:
    """MagicMock that behaves like urlopen()'s context-manager return value."""
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.read.return_value = body
    return resp


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://example.com", code=code, msg="x", hdrs=None, fp=None
    )


SAMPLE_IDX = b"""\
1:0:d=2026042906:UGRD:10 m above ground:6 hour fcst:
2:5000:d=2026042906:VGRD:10 m above ground:6 hour fcst:
3:10000:d=2026042906:TMP:2 m above ground:6 hour fcst:
4:15000:d=2026042906:DPT:2 m above ground:6 hour fcst:
"""


# ---------------------------------------------------------------------------
# Pure-function unit tests


def test_gfs_url_format():
    assert gfs_url("20260429", 6, 12) == (
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
        "gfs.20260429/06/atmos/gfs.t06z.pgrb2.0p25.f012"
    )


def test_hrrr_url_format():
    assert hrrr_url("20260429", 12, 1) == (
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/"
        "hrrr.20260429/conus/hrrr.t12z.wrfsfcf01.grib2"
    )


@patch("workers.weather_ingest.datetime")
def test_latest_cycle_respects_publish_lag(mock_datetime):
    """Mid-day, GFS picks the previous 6-hourly cycle accounting for 5h lag;
    HRRR picks the previous hourly cycle accounting for 2h lag."""
    mock_datetime.now.return_value = datetime(2026, 4, 29, 14, 30, tzinfo=timezone.utc)

    assert latest_cycle(SOURCES["gfs"]) == ("20260429", 6)
    assert latest_cycle(SOURCES["hrrr"]) == ("20260429", 12)


@patch("workers.weather_ingest.urllib.request.urlopen")
def test_fetch_ranges_parses_idx(mock_urlopen):
    mock_urlopen.return_value = _mock_resp(SAMPLE_IDX)

    ranges = fetch_ranges("http://x/y.idx", WIND_FIELDS)

    assert ranges == [(0, 4999), (5000, 9999)]


@patch("workers.weather_ingest.urllib.request.urlopen")
def test_fetch_ranges_handles_wind_field_at_end_of_idx(mock_urlopen):
    """When a wind field is the last entry, end is None (read to EOF)."""
    idx = (
        b"1:0:d=...:TMP:2 m above ground:fcst:\n"
        b"2:5000:d=...:UGRD:10 m above ground:fcst:\n"
    )
    mock_urlopen.return_value = _mock_resp(idx)

    assert fetch_ranges("http://x/y.idx", WIND_FIELDS) == [(5000, None)]


def test_clip_and_serialize_shape_and_keys():
    payload = clip_and_serialize(_synthetic_grid(), (40.0, 50.0, -94.0, -75.0))

    assert set(payload) == {
        "source", "reference_time", "valid_time", "bbox",
        "shape", "lats", "lons", "u", "v",
    }
    assert payload["source"] == "gfs"
    assert payload["shape"] == [len(payload["lats"]), len(payload["lons"])]
    assert payload["shape"] == [3, 3]
    assert payload["reference_time"] == "2026-04-29T06:00:00+00:00"
    assert payload["bbox"] == {
        "min_lat": 40.0, "max_lat": 50.0, "min_lon": -94.0, "max_lon": -75.0,
    }


def test_clip_and_serialize_raises_on_empty_bbox():
    with pytest.raises(ValueError, match="empty grid"):
        clip_and_serialize(_synthetic_grid(), (60.0, 70.0, -80.0, -70.0))


# ---------------------------------------------------------------------------
# Retry-logic tests


@patch("workers.weather_ingest.time.sleep")  # don't actually sleep
@patch("workers.weather_ingest.urllib.request.urlopen")
def test_fetch_ranges_retries_on_5xx(mock_urlopen, _mock_sleep):
    mock_urlopen.side_effect = [_http_error(503), _mock_resp(SAMPLE_IDX)]

    ranges = fetch_ranges("http://x/y.idx", WIND_FIELDS)

    assert ranges == [(0, 4999), (5000, 9999)]
    assert mock_urlopen.call_count == 2


@patch("workers.weather_ingest.time.sleep")
@patch("workers.weather_ingest.urllib.request.urlopen")
def test_fetch_ranges_does_not_retry_on_404(mock_urlopen, _mock_sleep):
    """404 means 'cycle not yet published' — fail fast, don't retry."""
    mock_urlopen.side_effect = _http_error(404)

    with pytest.raises(urllib.error.HTTPError) as exc:
        fetch_ranges("http://x/y.idx", WIND_FIELDS)

    assert exc.value.code == 404
    assert mock_urlopen.call_count == 1


@patch("workers.weather_ingest.time.sleep")
@patch("workers.weather_ingest.urllib.request.urlopen")
def test_fetch_ranges_gives_up_after_max_attempts(mock_urlopen, _mock_sleep):
    mock_urlopen.side_effect = [_http_error(503)] * 3

    with pytest.raises(urllib.error.HTTPError) as exc:
        fetch_ranges("http://x/y.idx", WIND_FIELDS)

    assert exc.value.code == 503
    assert mock_urlopen.call_count == 3


# ---------------------------------------------------------------------------
# Orchestration tests with everything mocked


@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_dry_run_writes_gz_to_disk(
    mock_latest, mock_fetch, mock_download, mock_parse
):
    mock_latest.return_value = ("20260429", 6)
    mock_fetch.return_value = [(0, 999), (1000, 1999)]
    mock_parse.return_value = _synthetic_grid()

    out_path = (
        Path(weather_ingest.__file__).parent.parent
        / "ingest_output"
        / "gfs_f006.json.gz"
    )
    out_path.unlink(missing_ok=True)
    try:
        payload = ingest("gfs", fhour=6, dry_run=True)

        assert out_path.exists()
        on_disk = json.loads(gzip.decompress(out_path.read_bytes()))
        assert on_disk["source"] == "gfs"
        assert on_disk["shape"] == payload["shape"]
    finally:
        out_path.unlink(missing_ok=True)


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_writes_redis_and_gcs(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 6)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid()

    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst

    mock_blob = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_storage_inst = MagicMock()
    mock_storage_inst.bucket.return_value = mock_bucket
    mock_storage_cls.return_value = mock_storage_inst

    ingest("gfs", fhour=6, dry_run=False)

    # Redis: weather:gfs:latest, TTL=6h, gzipped JSON bytes
    mock_redis_inst.setex.assert_called_once()
    key, ttl, blob = mock_redis_inst.setex.call_args[0]
    assert key == "weather:gfs:latest"
    assert ttl == 6 * 3600
    decoded = json.loads(gzip.decompress(blob))
    assert decoded["source"] == "gfs"

    # GCS: gfs/{cycle_iso}.json.gz in fake-bucket
    mock_storage_inst.bucket.assert_called_with("fake-bucket")
    mock_bucket.blob.assert_called_with("gfs/20260429T0600Z.json.gz")
    mock_blob.upload_from_string.assert_called_once()


@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_raises_when_idx_has_no_wind_fields(mock_latest, mock_fetch):
    mock_latest.return_value = ("20260429", 6)
    mock_fetch.return_value = []

    with pytest.raises(RuntimeError, match="No matching wind fields"):
        ingest("gfs", fhour=6, dry_run=True)


@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_cleans_up_tempfile_on_parse_failure(
    mock_latest, mock_fetch, mock_download, mock_parse
):
    """Regression: tempfile must be unlinked even when parse_grib raises.
    Catches the Windows PermissionError class of bug from Step 2."""
    mock_latest.return_value = ("20260429", 6)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.side_effect = ValueError("corrupt grib")

    captured: list[Path] = []
    mock_download.side_effect = lambda url, ranges, out: captured.append(Path(out))

    with pytest.raises(ValueError, match="corrupt grib"):
        ingest("gfs", fhour=6, dry_run=True)

    assert captured, "download_grib should have been called"
    assert not captured[0].exists(), "tempfile should be cleaned up"
