# backend/tests/test_weather_ingest.py
"""Tests for the weather ingestion worker (rolling-forecast variant).

Covers:
  - URL builders for HRRR/GFS
  - .idx parsing + range extraction
  - retry behaviour (5xx retry, 404 fast-fail)
  - clip_and_serialize shape + empty-bbox guard
  - region validation (unknown / wrong source for region)
  - per-region resolution propagation to the parser
  - single-fhour ingest() — per-fhour key + alias on default_fhour only
  - ingest_cycle() — full sequence write, manifest, cycles sorted set, 404 stop
"""
from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import numpy as np
import pytest

from app.services.grib import WindGrid
from workers.weather_ingest import (
    SOURCES,
    WIND_FIELDS,
    clip_and_serialize,
    fetch_ranges,
    gfs_url,
    hrrr_url,
    ingest,
    ingest_cycle,
    latest_cycle,
)


# ─── Synthetic grid helpers ──────────────────────────────────────────────


def _synthetic_grid_conus(source: str = "gfs", ref_hour: int = 6) -> WindGrid:
    lats = np.array([24.0, 37.0, 50.0])
    lons = np.array([-126.0, -96.0, -66.0])
    u = np.zeros((3, 3), dtype=np.float32)
    v = np.full((3, 3), 5.0, dtype=np.float32)
    return WindGrid(
        lats=lats, lons=lons, u=u, v=v,
        reference_time=datetime(2026, 4, 29, ref_hour, tzinfo=timezone.utc),
        valid_time=datetime(2026, 4, 29, ref_hour + 1, tzinfo=timezone.utc),
        source=source,
    )


def _synthetic_grid_sf_bay() -> WindGrid:
    lats = np.array([37.4, 37.7, 38.0])
    lons = np.array([-122.7, -122.3, -121.9])
    u = np.zeros((3, 3), dtype=np.float32)
    v = np.full((3, 3), 5.0, dtype=np.float32)
    return WindGrid(
        lats=lats, lons=lons, u=u, v=v,
        reference_time=datetime(2026, 4, 29, 12, tzinfo=timezone.utc),
        valid_time=datetime(2026, 4, 29, 13, tzinfo=timezone.utc),
        source="hrrr",
    )


SAMPLE_IDX = (
    b"1:0:d=2026042906:UGRD:10 m above ground:fcst:\n"
    b"2:5000:d=2026042906:VGRD:10 m above ground:fcst:\n"
    b"3:10000:d=2026042906:TMP:2 m above ground:fcst:\n"
)


def _mock_resp(body: bytes):
    resp = MagicMock()
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: False
    resp.read.return_value = body
    return resp


def _http_error(code: int) -> HTTPError:
    return HTTPError("http://x", code, f"err{code}", {}, BytesIO(b""))


# ─── URL builders ────────────────────────────────────────────────────────


def test_gfs_url_format():
    url = gfs_url("20260429", 6, 12)
    assert url.endswith("gfs.t06z.pgrb2.0p25.f012")
    assert "/gfs.20260429/06/atmos/" in url


def test_hrrr_url_format():
    url = hrrr_url("20260429", 12, 1)
    assert url.endswith("hrrr.t12z.wrfsfcf01.grib2")
    assert "/hrrr.20260429/conus/" in url


# ─── latest_cycle math ───────────────────────────────────────────────────


@patch("workers.weather_ingest.datetime")
def test_latest_cycle_subtracts_publish_lag(mock_dt):
    mock_dt.now.return_value = datetime(2026, 4, 29, 13, 30, tzinfo=timezone.utc)
    mock_dt.strftime = datetime.strftime
    date, cycle = latest_cycle(SOURCES["hrrr"])  # publish_lag=2h
    # 13:30 - 2h = 11:30 → cycle hour bucket = 11
    assert cycle == 11
    assert date == "20260429"


# ─── .idx parsing ────────────────────────────────────────────────────────


@patch("workers.weather_ingest.urllib.request.urlopen")
def test_fetch_ranges_extracts_wind_fields(mock_urlopen):
    mock_urlopen.return_value = _mock_resp(SAMPLE_IDX)
    ranges = fetch_ranges("http://x/y.idx", WIND_FIELDS)
    assert ranges == [(0, 4999), (5000, 9999)]


@patch("workers.weather_ingest.urllib.request.urlopen")
def test_fetch_ranges_handles_open_ended_last_field(mock_urlopen):
    idx = (
        b"1:0:d=...:TMP:2 m above ground:fcst:\n"
        b"2:5000:d=...:UGRD:10 m above ground:fcst:\n"
    )
    mock_urlopen.return_value = _mock_resp(idx)
    assert fetch_ranges("http://x/y.idx", WIND_FIELDS) == [(5000, None)]


# ─── Retry behaviour ─────────────────────────────────────────────────────


@patch("workers.weather_ingest.time.sleep")
@patch("workers.weather_ingest.urllib.request.urlopen")
def test_fetch_ranges_retries_on_5xx(mock_urlopen, _mock_sleep):
    mock_urlopen.side_effect = [_http_error(503), _mock_resp(SAMPLE_IDX)]
    ranges = fetch_ranges("http://x/y.idx", WIND_FIELDS)
    assert ranges == [(0, 4999), (5000, 9999)]
    assert mock_urlopen.call_count == 2


@patch("workers.weather_ingest.time.sleep")
@patch("workers.weather_ingest.urllib.request.urlopen")
def test_fetch_ranges_does_not_retry_on_404(mock_urlopen, _mock_sleep):
    """404 = 'cycle not yet published' — fail fast."""
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


# ─── clip + serialize ───────────────────────────────────────────────────


def test_clip_and_serialize_shape_and_keys():
    payload = clip_and_serialize(
        _synthetic_grid_conus(), (24.0, 50.0, -126.0, -66.0),
    )
    assert set(payload) == {
        "source", "reference_time", "valid_time", "bbox",
        "shape", "lats", "lons", "u", "v",
    }
    assert payload["source"] == "gfs"
    assert payload["shape"] == [3, 3]
    assert payload["bbox"] == {
        "min_lat": 24.0, "max_lat": 50.0,
        "min_lon": -126.0, "max_lon": -66.0,
    }


def test_clip_and_serialize_raises_on_empty_bbox():
    with pytest.raises(ValueError, match="empty grid"):
        clip_and_serialize(
            _synthetic_grid_conus(), (60.0, 70.0, -80.0, -70.0),
        )


# ─── Region validation ──────────────────────────────────────────────────


def test_ingest_rejects_unknown_region():
    with pytest.raises(ValueError, match="unknown region"):
        ingest("gfs", region_name="atlantis", dry_run=True)


def test_ingest_rejects_hrrr_for_hawaii():
    """Hawaii is GFS-only — HRRR doesn't cover it."""
    with pytest.raises(ValueError, match="not configured"):
        ingest("hrrr", region_name="hawaii", dry_run=True)


def test_ingest_rejects_gfs_for_venue():
    """Venues are HRRR-only."""
    with pytest.raises(ValueError, match="not configured"):
        ingest("gfs", region_name="sf_bay", dry_run=True)


# ─── Resolution propagation ─────────────────────────────────────────────


@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_passes_per_region_resolution_for_conus_hrrr(
    mock_latest, mock_fetch, mock_download, mock_parse,
):
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
    mock_latest, mock_fetch, mock_download, mock_parse,
):
    mock_latest.return_value = ("20260429", 12)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_sf_bay()

    ingest("hrrr", region_name="sf_bay", fhour=1, dry_run=True)

    _, kwargs = mock_parse.call_args
    assert kwargs["target_resolution_deg"] == 0.027


# ─── Single-fhour ingest() — Redis + GCS write contract ─────────────────


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_default_fhour_writes_per_fhour_key_AND_latest_alias(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    """Default fhour gets two Redis keys: per-fhour + alias for backwards compat."""
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 6)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_conus(source="gfs", ref_hour=6)

    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst
    mock_bucket = MagicMock()
    mock_storage_cls.return_value.bucket.return_value = mock_bucket

    # GFS default_fhour is 6 — the alias gets written.
    ingest("gfs", region_name="conus", fhour=6, dry_run=False)

    setex_keys = [c.args[0] for c in mock_redis_inst.setex.call_args_list]
    assert "weather:gfs:conus:20260429T0600Z:f006" in setex_keys
    assert "weather:gfs:conus:latest" in setex_keys
    # All setex calls use the new GFS TTL of 12h.
    setex_ttls = [c.args[1] for c in mock_redis_inst.setex.call_args_list]
    assert all(ttl == 12 * 3600 for ttl in setex_ttls)


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_non_default_fhour_writes_per_fhour_only_no_alias(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    """Non-default fhour writes only the per-fhour key, not the alias.

    This protects the :latest endpoint's contract — only F01 (HRRR) or F06
    (GFS) gets aliased there. Otherwise the wind overlay would jump around
    as different fhours overwrote the alias.
    """
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 12)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_conus(source="hrrr", ref_hour=12)

    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst
    mock_storage_cls.return_value.bucket.return_value = MagicMock()

    # HRRR default_fhour is 1; we explicitly pass 5 → no alias should be written.
    ingest("hrrr", region_name="conus", fhour=5, dry_run=False)

    setex_keys = [c.args[0] for c in mock_redis_inst.setex.call_args_list]
    assert "weather:hrrr:conus:20260429T1200Z:f005" in setex_keys
    assert "weather:hrrr:conus:latest" not in setex_keys


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_writes_per_fhour_gcs_archive(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    """GCS archive path is now per-fhour: {src}/{region}/{cycle}/f{NNN}.json.gz"""
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 12)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_conus(source="hrrr", ref_hour=12)

    mock_redis_cls.return_value = MagicMock()
    mock_bucket = MagicMock()
    mock_storage_cls.return_value.bucket.return_value = mock_bucket

    ingest("hrrr", region_name="conus", fhour=3, dry_run=False)

    mock_bucket.blob.assert_any_call("hrrr/conus/20260429T1200Z/f003.json.gz")


# ─── ingest_cycle() — full forecast sequence ────────────────────────────


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_cycle_writes_all_fhours_for_hrrr(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    """HRRR cycle ingest writes 19 per-fhour keys (F00..F18) + manifest + cycles index."""
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 12)
    mock_fetch.return_value = [(0, 999)]
    # Each parse call returns the same synthetic grid; the worker uses
    # the reference_time on the grid, so each "fhour" resolves to the
    # same cycle_iso (which is what we want for a real cycle).
    mock_parse.return_value = _synthetic_grid_conus(source="hrrr", ref_hour=12)

    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst
    mock_storage_cls.return_value.bucket.return_value = MagicMock()

    manifest = ingest_cycle("hrrr", region_name="conus", dry_run=False)

    setex_keys = [c.args[0] for c in mock_redis_inst.setex.call_args_list]
    # 19 per-fhour keys
    for fh in range(0, 19):
        assert f"weather:hrrr:conus:20260429T1200Z:f{fh:03d}" in setex_keys
    # Manifest key
    assert "weather:hrrr:conus:20260429T1200Z:manifest" in setex_keys
    # :latest alias (F01 is HRRR's default_fhour)
    assert "weather:hrrr:conus:latest" in setex_keys

    # Cycles sorted set updated.
    mock_redis_inst.zadd.assert_called()
    zadd_args = mock_redis_inst.zadd.call_args.args
    assert zadd_args[0] == "weather:hrrr:conus:cycles"
    assert "20260429T1200Z" in zadd_args[1]

    # Trim keeps only newest 8 cycles.
    mock_redis_inst.zremrangebyrank.assert_called_with(
        "weather:hrrr:conus:cycles", 0, -9,
    )

    # Returned manifest dict shape.
    assert manifest["source"] == "hrrr"
    assert manifest["region"] == "conus"
    assert manifest["fhours"] == list(range(0, 19))
    assert len(manifest["valid_times"]) == 19


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_cycle_writes_gfs_at_3h_step(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    """GFS cycle: F000..F120 step 3 → 41 fhours."""
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 6)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_conus(source="gfs", ref_hour=6)

    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst
    mock_storage_cls.return_value.bucket.return_value = MagicMock()

    manifest = ingest_cycle("gfs", region_name="conus", dry_run=False)

    assert manifest["fhours"] == list(range(0, 121, 3))
    assert len(manifest["fhours"]) == 41


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_cycle_stops_on_404_and_writes_partial_manifest(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, monkeypatch,
):
    """If F12 isn't published yet, cycle ingest stops there and finalises
    the manifest with the fhours it did get. This matches NOAA's incremental
    publishing — F00 lands first, then later fhours stream in over ~30 min.
    """
    monkeypatch.setenv("REDIS_HOST", "fake-redis")
    monkeypatch.setenv("GCS_WEATHER_BUCKET", "fake-bucket")

    mock_latest.return_value = ("20260429", 12)

    # First 5 fhours fine, sixth returns 404.
    def _fetch_side_effect(*args, **kwargs):
        # Each call counts as one fhour lookup. Use a closure counter.
        _fetch_side_effect.calls += 1
        if _fetch_side_effect.calls > 5:
            raise _http_error(404)
        return [(0, 999)]
    _fetch_side_effect.calls = 0
    mock_fetch.side_effect = _fetch_side_effect

    mock_parse.return_value = _synthetic_grid_conus(source="hrrr", ref_hour=12)
    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst
    mock_storage_cls.return_value.bucket.return_value = MagicMock()

    manifest = ingest_cycle("hrrr", region_name="conus", dry_run=False)

    # Got 5 fhours (F00..F04) before the 404.
    assert manifest["fhours"] == [0, 1, 2, 3, 4]
    # Manifest still gets written with what we have.
    setex_keys = [c.args[0] for c in mock_redis_inst.setex.call_args_list]
    assert "weather:hrrr:conus:20260429T1200Z:manifest" in setex_keys


@patch("workers.weather_ingest.storage.Client")
@patch("workers.weather_ingest.redis.Redis")
@patch("workers.weather_ingest.parse_grib_to_wind_grid")
@patch("workers.weather_ingest.download_grib")
@patch("workers.weather_ingest.fetch_ranges")
@patch("workers.weather_ingest.latest_cycle")
def test_ingest_cycle_dry_run_writes_manifest_to_disk(
    mock_latest, mock_fetch, mock_download, mock_parse,
    mock_redis_cls, mock_storage_cls, tmp_path, monkeypatch,
):
    """Dry-run path doesn't touch Redis/GCS; manifest written to ingest_output/."""
    mock_latest.return_value = ("20260429", 12)
    mock_fetch.return_value = [(0, 999)]
    mock_parse.return_value = _synthetic_grid_conus(source="hrrr", ref_hour=12)

    mock_redis_inst = MagicMock()
    mock_redis_cls.return_value = mock_redis_inst
    mock_storage_cls.return_value.bucket.return_value = MagicMock()

    manifest = ingest_cycle("hrrr", region_name="conus", dry_run=True)

    mock_redis_inst.setex.assert_not_called()
    mock_redis_inst.zadd.assert_not_called()
    assert manifest["fhours"] == list(range(0, 19))


# ─── Source dataclass — new fields exist with sane defaults ─────────────


def test_sources_have_fhour_range_configured():
    """fhour_min/max/step were added in the rolling-forecast refactor."""
    hrrr = SOURCES["hrrr"]
    assert hrrr.fhour_min == 0
    assert hrrr.fhour_max == 18
    assert hrrr.fhour_step == 1
    assert hrrr.fhour_range() == list(range(0, 19))

    gfs = SOURCES["gfs"]
    assert gfs.fhour_min == 0
    assert gfs.fhour_max == 120
    assert gfs.fhour_step == 3
    assert len(gfs.fhour_range()) == 41


def test_sources_ttls_longer_than_cycle_intervals():
    """TTL > cycle interval prevents per-fhour keys from expiring before
    the next cycle replaces them — important for the cycles index to stay
    consistent with the actual key set.
    """
    hrrr = SOURCES["hrrr"]
    assert hrrr.cache_ttl_seconds > hrrr.cycle_step_hours * 3600
    gfs = SOURCES["gfs"]
    assert gfs.cache_ttl_seconds > gfs.cycle_step_hours * 3600