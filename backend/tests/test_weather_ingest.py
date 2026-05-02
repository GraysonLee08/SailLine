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


def _synthetic_grid_conus(source: str = "gfs", ref_hour: int = 6) -> WindGrid:
    """3x3 wind grid inside the CONUS bbox."""
    return WindGrid(
        lats=np.array([35.0, 40.0, 45.0]),
        lons=np.array([-100.0, -90.0, -80.0]),
        u=np.full((3, 3), 5.0, dtype=np.float32),
        v=np.full((3, 3), -3.0, dtype=np.float32),
        reference_time=datetime(2026, 4, 29, ref_hour, tzinfo=timezone.utc),
        valid_time=datetime(2026, 4, 29, ref_hour + 6, tzinfo=timezone.utc),
        source=source,
    )


def _synthetic_grid_sf_bay() -> WindGrid:
    """3x3 wind grid inside the sf_bay venue bbox."""
    return WindGrid(
        lats=np.array([37.5, 37.8, 38.1]),
        lons=np.array([-122.5, -122.3, -122.0]),
        u=np.full((3, 3), 4.0, dtype=np.float32),
        v=np.full((3, 3), -2.0, dtype=np.float32),
        reference_time=datetime(2026, 4, 29, 12, tzinfo=timezone.utc),
        valid_time=datetime(2026, 4, 29, 13, tzinfo=timezone.utc),
        source="hrrr",
    )


def _synthetic_grid_hawaii() -> WindGrid:
    """3x3 wind grid inside the hawaii bbox."""
    return WindGrid(
        lats=np.array([19.0, 20.5, 22.0]),
        lons=np.array([-160.0, -158.0, -156.0]),
        u=np.full((3, 3), 6.0, dtype=np.float32),
        v=np.full((3, 3), -4.0, dtype=np.float32),
        reference_time=datetime(2026, 4, 29, 18, tzinfo=timezone.utc),
        valid_time=datetime(2026, 4, 30, 0, tzinfo=timezone.utc),
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
    payload = clip_and_serialize(_synthetic_grid_conus(), (24.0, 50.0, -126.0, -66.0))

    assert set(payload) == {
        "source", "reference_time", "valid_time", "bbox",
        "shape", "lats", "lons", "u", "v",
    }
    assert payload["source"] == "gfs"
    assert payload["shape"] == [len(payload["lats"]), len(payload["lons"])]
    assert payload["shape"] == [3, 3]
    assert payload["bbox"] == {
        "min_lat": 24.0, "max_lat": 50.0, "min_lon": -126.0, "max_lon": -66.0,
    }


def test_clip_and_serialize_raises_on_empty_bbox():
    with pytest.raises(ValueError, match="empty grid"):
        clip_and_serialize(_synthetic_grid_conus(), (60.0, 70.0, -80.0, -70.0))


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
# Region validation tests


def test_ingest_rejects_unknown_region():
    with pytest.raises(ValueError, match="unknown region"):
        ingest("gfs", region_name="atlantis", dry_run=True)


def test_ingest_rejects_hrrr_for_hawaii():
    """Hawaii is GFS-only because HRRR doesn't cover it."""
    with pytest.raises(ValueError, match="not configured"):
        ingest("hrrr", region_name="hawaii", dry_run=True)


def test_ingest_rejects_gfs_for_venue():
    """Venues are HRRR-only."""
    with pytest.raises(ValueError, match="not configured"):
        ingest("gfs", region_name="sf_bay", dry_run=True)


# ---------------------------------------------------------------------------
# Resolution propagation — the key new behavior in this refactor


@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_passes_per_region_resolution_for_conus_hrrr(
    mock_latest, mock_fetch, mock_download, mock_parse
):
    """conus + HRRR should request 0.10° regridding from the parser."""
    mock_latest.return_value = ("20260429", 12)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_conus(source="hrrr", ref_hour=12)

    ingest("hrrr", region_name="conus", fhour=1, dry_run=True)

    _, kwargs = mock_parse.call_args
    assert kwargs["target_resolution_deg"] == 0.10
    assert kwargs["target_bbox"] == (24.0, 50.0, -126.0, -66.0)


@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_passes_per_region_resolution_for_venue_hrrr(
    mock_latest, mock_fetch, mock_download, mock_parse
):
    """sf_bay + HRRR should request native 0.027° regridding."""
    mock_latest.return_value = ("20260429", 12)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_sf_bay()

    ingest("hrrr", region_name="sf_bay", fhour=1, dry_run=True)

    _, kwargs = mock_parse.call_args
    assert kwargs["target_resolution_deg"] == 0.027


# ---------------------------------------------------------------------------
# End-to-end orchestration tests


@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_dry_run_writes_gz_to_disk(
    mock_latest, mock_fetch, mock_download, mock_parse
):
    mock_latest.return_value = ("20260429", 6)
    mock_fetch.return_value = [(0, 999), (1000, 1999)]
    mock_parse.return_value = _synthetic_grid_conus()

    out_path = (
        Path(weather_ingest.__file__).parent.parent
        / "ingest_output"
        / "gfs_conus_f006.json.gz"
    )
    out_path.unlink(missing_ok=True)
    try:
        payload = ingest("gfs", region_name="conus", fhour=6, dry_run=True)

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
def test_ingest_writes_conus_keys(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 6)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_conus()

    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst

    mock_blob = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_storage_inst = MagicMock()
    mock_storage_inst.bucket.return_value = mock_bucket
    mock_storage_cls.return_value = mock_storage_inst

    ingest("gfs", region_name="conus", fhour=6, dry_run=False)

    key, ttl, _ = mock_redis_inst.setex.call_args[0]
    assert key == "weather:gfs:conus:latest"
    assert ttl == 6 * 3600
    mock_bucket.blob.assert_called_with("gfs/conus/20260429T0600Z.json.gz")


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_writes_venue_keys(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 12)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_sf_bay()

    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst
    mock_blob = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_storage_inst = MagicMock()
    mock_storage_inst.bucket.return_value = mock_bucket
    mock_storage_cls.return_value = mock_storage_inst

    ingest("hrrr", region_name="sf_bay", fhour=1, dry_run=False)

    key, ttl, _ = mock_redis_inst.setex.call_args[0]
    assert key == "weather:hrrr:sf_bay:latest"
    assert ttl == 1 * 3600
    mock_bucket.blob.assert_called_with("hrrr/sf_bay/20260429T1200Z.json.gz")


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_writes_hawaii_keys(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 18)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_hawaii()

    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst
    mock_blob = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_storage_inst = MagicMock()
    mock_storage_inst.bucket.return_value = mock_bucket
    mock_storage_cls.return_value = mock_storage_inst

    ingest("gfs", region_name="hawaii", fhour=6, dry_run=False)

    key, _, _ = mock_redis_inst.setex.call_args[0]
    assert key == "weather:gfs:hawaii:latest"
    mock_bucket.blob.assert_called_with("gfs/hawaii/20260429T1800Z.json.gz")


@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_raises_when_idx_has_no_wind_fields(mock_latest, mock_fetch):
    mock_latest.return_value = ("20260429", 6)
    mock_fetch.return_value = []

    with pytest.raises(RuntimeError, match="No matching wind fields"):
        ingest("gfs", region_name="conus", fhour=6, dry_run=True)


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
        ingest("gfs", region_name="conus", fhour=6, dry_run=True)

    assert captured, "download_grib should have been called"
    assert not captured[0].exists(), "tempfile should be cleaned up"
