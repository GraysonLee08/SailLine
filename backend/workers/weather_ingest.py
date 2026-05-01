"""NOAA weather ingestion -> wind grid JSON, clipped to a region bbox.

Production: runs as a Cloud Run Job per (source, region), writes to Redis + GCS.
Local: --dry-run flag writes JSON to ./ingest_output/ instead.

Usage (from backend/):
    python -m workers.weather_ingest gfs --dry-run
    python -m workers.weather_ingest hrrr --region chesapeake --dry-run
    python -m workers.weather_ingest gfs --region hawaii --fhour 12 --dry-run

--region defaults to great_lakes so existing infra (the original
sailline-ingest-{source} Cloud Run Jobs that don't yet pass --region)
continues to work after deploy. New region-specific jobs pass --region
explicitly. See docs/multi-region-rollout.md.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import gzip
import redis
from google.cloud import storage

import numpy as np

from app.regions import REGIONS, Region
from app.services.grib import WindGrid, parse_grib_to_wind_grid

# Kept for back-compat with tests/test_weather_ingest_live.py and any caller
# that imported it. New code should reach into app.regions instead.
DEFAULT_BBOX = REGIONS["great_lakes"].bbox

WIND_FIELDS = (":UGRD:10 m above ground:", ":VGRD:10 m above ground:")


# ---------------------------------------------------------------------------
# Source configuration


def gfs_url(date: str, cycle: int, fhour: int) -> str:
    """GFS 0.25° global. Files run ~500MB each — we use byte ranges."""
    return (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
        f"gfs.{date}/{cycle:02d}/atmos/gfs.t{cycle:02d}z.pgrb2.0p25.f{fhour:03d}"
    )


def hrrr_url(date: str, cycle: int, fhour: int) -> str:
    """HRRR 3km CONUS. Surface file (wrfsfcf) — has 10m winds."""
    return (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/"
        f"hrrr.{date}/conus/hrrr.t{cycle:02d}z.wrfsfcf{fhour:02d}.grib2"
    )


@dataclass(frozen=True)
class Source:
    name: str
    url_fn: Callable[[str, int, int], str]
    cycle_step_hours: int
    publish_lag_hours: int
    default_fhour: int
    cache_ttl_seconds: int        # Redis TTL
    target_resolution_deg: float  # used only for curvilinear (HRRR); GFS ignores


SOURCES: dict[str, Source] = {
    "gfs":  Source("gfs",  gfs_url,  6, 5, 6,  cache_ttl_seconds=6 * 3600, target_resolution_deg=0.25),
    "hrrr": Source("hrrr", hrrr_url, 1, 2, 1,  cache_ttl_seconds=1 * 3600, target_resolution_deg=0.10),
}


def latest_cycle(source: Source) -> tuple[str, int]:
    """Pick the most recent run that should be fully published."""
    now = datetime.now(timezone.utc) - timedelta(hours=source.publish_lag_hours)
    cycle = (now.hour // source.cycle_step_hours) * source.cycle_step_hours
    return now.strftime("%Y%m%d"), cycle


# ---------------------------------------------------------------------------
# Byte-range download via .idx (shared with scripts/download_fixture.py)


def _urlopen_with_retries(req, *, timeout: int, max_attempts: int = 3):
    """urlopen with exponential backoff on 5xx and network errors.

    4xx (including 404 'cycle not yet published') propagates immediately so
    callers can fail fast instead of waiting through retries on a permanent
    error. Returns the urlopen response (caller is responsible for using it
    as a context manager).
    """
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == max_attempts:
                raise
            print(f"  retry {attempt}/{max_attempts} after HTTP {e.code}", flush=True)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == max_attempts:
                raise
            print(f"  retry {attempt}/{max_attempts} after {type(e).__name__}", flush=True)
        time.sleep(delay)
        delay *= 2


def fetch_ranges(idx_url: str, fields: tuple[str, ...]) -> list[tuple[int, int | None]]:
    """Parse a NOAA .idx file, return (start, end) byte ranges for matching fields."""
    with _urlopen_with_retries(idx_url, timeout=30) as resp:
        lines = [ln for ln in resp.read().decode("ascii").splitlines() if ln.strip()]

    entries: list[tuple[int, int, str]] = []
    for ln in lines:
        parts = ln.split(":", 2)
        entries.append((int(parts[0]), int(parts[1]), ":" + parts[2]))

    ranges: list[tuple[int, int | None]] = []
    for i, (_, offset, descriptor) in enumerate(entries):
        if any(f in descriptor for f in fields):
            end = entries[i + 1][1] - 1 if i + 1 < len(entries) else None
            ranges.append((offset, end))
    return ranges


def download_grib(grib_url: str, ranges: list[tuple[int, int | None]], out: Path) -> None:
    """Append byte ranges from grib_url to out as a valid concatenated GRIB2 file."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        for start, end in ranges:
            header = f"bytes={start}-{end if end is not None else ''}"
            req = urllib.request.Request(grib_url, headers={"Range": header})
            with _urlopen_with_retries(req, timeout=120) as resp:
                f.write(resp.read())


# ---------------------------------------------------------------------------
# Bbox clipping + JSON serialization


def clip_and_serialize(
    grid: WindGrid, bbox: tuple[float, float, float, float]
) -> dict:
    """Clip wind grid to bbox, round to 2 decimals, return JSON-ready dict."""
    min_lat, max_lat, min_lon, max_lon = bbox
    lat_mask = (grid.lats >= min_lat) & (grid.lats <= max_lat)
    lon_mask = (grid.lons >= min_lon) & (grid.lons <= max_lon)

    if not lat_mask.any() or not lon_mask.any():
        raise ValueError(f"bbox {bbox} produced empty grid")

    lats = grid.lats[lat_mask]
    lons = grid.lons[lon_mask]
    u = grid.u[np.ix_(lat_mask, lon_mask)]
    v = grid.v[np.ix_(lat_mask, lon_mask)]

    return {
        "source": grid.source,
        "reference_time": grid.reference_time.isoformat(),
        "valid_time": grid.valid_time.isoformat(),
        "bbox": {
            "min_lat": min_lat, "max_lat": max_lat,
            "min_lon": min_lon, "max_lon": max_lon,
        },
        "shape": list(u.shape),
        "lats": [round(float(x), 4) for x in lats],
        "lons": [round(float(x), 4) for x in lons],
        "u": np.round(u, 2).tolist(),
        "v": np.round(v, 2).tolist(),
    }


# ---------------------------------------------------------------------------
# Pipeline


def _write_redis(key: str, ttl: int, blob: bytes) -> None:
    r = redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", 6379)),
        socket_timeout=10,
    )
    r.setex(key, ttl, blob)


def _write_gcs(source_name: str, region_name: str, cycle_iso: str, blob: bytes) -> str:
    bucket_name = os.environ["GCS_WEATHER_BUCKET"]
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    path = f"{source_name}/{region_name}/{cycle_iso}.json.gz"
    obj = bucket.blob(path)
    obj.content_encoding = "gzip"
    obj.upload_from_string(blob, content_type="application/json")
    return f"gs://{bucket_name}/{path}"


def ingest(
    source_name: str,
    region_name: str = "great_lakes",
    fhour: int | None = None,
    dry_run: bool = False,
) -> dict:
    if region_name not in REGIONS:
        raise ValueError(
            f"unknown region: {region_name}. valid: {sorted(REGIONS)}"
        )
    region: Region = REGIONS[region_name]

    if source_name not in region.sources:
        raise ValueError(
            f"source {source_name!r} not configured for region {region_name!r}. "
            f"valid: {list(region.sources)}"
        )

    source = SOURCES[source_name]
    if fhour is None:
        fhour = source.default_fhour

    bbox = region.bbox

    date, cycle = latest_cycle(source)
    grib_url = source.url_fn(date, cycle, fhour)
    print(f"[{source.name}/{region.name}] cycle={date} {cycle:02d}Z fhour={fhour:03d}", flush=True)
    print(f"[{source.name}/{region.name}] url={grib_url}", flush=True)

    ranges = fetch_ranges(f"{grib_url}.idx", WIND_FIELDS)
    if not ranges:
        raise RuntimeError(f"No matching wind fields in {grib_url}.idx")

    fd, tmp_path_str = tempfile.mkstemp(suffix=".grib2")
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    try:
        download_grib(grib_url, ranges, tmp_path)
        print(
            f"[{source.name}/{region.name}] downloaded "
            f"{tmp_path.stat().st_size / 1024:.1f} KB",
            flush=True,
        )
        grid = parse_grib_to_wind_grid(
            tmp_path,
            source=source.name,
            target_bbox=bbox,
            target_resolution_deg=source.target_resolution_deg,
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    payload = clip_and_serialize(grid, bbox)
    print(
        f"[{source.name}/{region.name}] grid {payload['shape'][0]}x{payload['shape'][1]} "
        f"({payload['lats'][0]:.2f}..{payload['lats'][-1]:.2f}N, "
        f"{payload['lons'][0]:.2f}..{payload['lons'][-1]:.2f}E)",
        flush=True,
    )

    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_gz = gzip.compress(payload_json)
    print(
        f"[{source.name}/{region.name}] payload {len(payload_json) / 1024:.1f} KB "
        f"-> gz {len(payload_gz) / 1024:.1f} KB",
        flush=True,
    )

    if dry_run:
        out_dir = Path(__file__).parent.parent / "ingest_output"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / f"{source.name}_{region.name}_f{fhour:03d}.json.gz"
        out_path.write_bytes(payload_gz)
        print(f"[{source.name}/{region.name}] dry-run -> {out_path}", flush=True)
    else:
        cycle_iso = grid.reference_time.strftime("%Y%m%dT%H%MZ")
        _write_redis(
            f"weather:{source.name}:{region.name}:latest",
            source.cache_ttl_seconds,
            payload_gz,
        )
        gcs_uri = _write_gcs(source.name, region.name, cycle_iso, payload_gz)
        print(
            f"[{source.name}/{region.name}] redis ttl={source.cache_ttl_seconds}s "
            f"gcs={gcs_uri}",
            flush=True,
        )

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="NOAA weather ingestion worker")
    parser.add_argument("source", choices=sorted(SOURCES.keys()))
    parser.add_argument(
        "--region",
        default="great_lakes",
        choices=sorted(REGIONS.keys()),
        help="Region to clip to (default: great_lakes)",
    )
    parser.add_argument("--fhour", type=int, help="Forecast hour (default: source-specific)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write JSON to ./ingest_output/ instead of Redis/GCS",
    )
    args = parser.parse_args()
    ingest(args.source, region_name=args.region, fhour=args.fhour, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
