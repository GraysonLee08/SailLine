# backend/workers/weather_ingest.py
"""NOAA weather ingestion -> wind grid JSON, clipped to a region bbox.

Production: runs as a Cloud Run Job per (source, region). One invocation
ingests the full forecast sequence for the latest cycle (HRRR F00-F18,
GFS F000-F120 at 3h step) and writes per-fhour keys to Redis + GCS.
Local: --dry-run writes JSON to ./ingest_output/ instead.

Usage (from backend/):
    python -m workers.weather_ingest hrrr --region conus --dry-run
    python -m workers.weather_ingest gfs --region conus --dry-run
    python -m workers.weather_ingest hrrr --region conus --fhour 1 --dry-run

Redis key shape (post-rolling-forecast):
    weather:{source}:{region}:{cycle}:f{fhour:03d}   gzipped JSON wind grid
    weather:{source}:{region}:{cycle}:manifest       JSON: cycle, fhours, valid_times
    weather:{source}:{region}:cycles                 sorted set, score=cycle epoch
    weather:{source}:{region}:latest                 alias to newest cycle's default fhour
                                                      (preserves /api/weather contract)
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import redis
from google.cloud import storage

from app.regions import REGIONS, Region
from app.services.grib import parse_grib_to_wind_grid

WIND_FIELDS = (":UGRD:10 m above ground:", ":VGRD:10 m above ground:")


# ---------------------------------------------------------------------------
# Source configuration


def gfs_url(date: str, cycle: int, fhour: int) -> str:
    return (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
        f"gfs.{date}/{cycle:02d}/atmos/gfs.t{cycle:02d}z.pgrb2.0p25.f{fhour:03d}"
    )


def hrrr_url(date: str, cycle: int, fhour: int) -> str:
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
    default_fhour: int           # the fhour written to `:latest` for backwards compat
    cache_ttl_seconds: int       # Redis TTL on per-fhour grids
    fhour_min: int               # inclusive
    fhour_max: int               # inclusive — full forecast horizon
    fhour_step: int              # 1 for HRRR; 3 for GFS to keep download cost bounded

    def fhour_range(self) -> list[int]:
        return list(range(self.fhour_min, self.fhour_max + 1, self.fhour_step))


SOURCES: dict[str, Source] = {
    # HRRR: F00-F18 hourly. 19 files per cycle.
    "hrrr": Source(
        "hrrr", hrrr_url, 1, 2, 1,
        cache_ttl_seconds=2 * 3600,    # cycle TTL longer than cycle interval
        fhour_min=0, fhour_max=18, fhour_step=1,
    ),
    # GFS: F000-F120 every 3h. 41 files per cycle. Plenty for Mac-length races.
    # Past 120h GFS shifts to 3h native anyway and is rarely worth the bandwidth.
    "gfs":  Source(
        "gfs", gfs_url, 6, 5, 6,
        cache_ttl_seconds=12 * 3600,
        fhour_min=0, fhour_max=120, fhour_step=3,
    ),
}


def latest_cycle(source: Source) -> tuple[str, int]:
    """Most recent run that should be fully published."""
    now = datetime.now(timezone.utc) - timedelta(hours=source.publish_lag_hours)
    cycle = (now.hour // source.cycle_step_hours) * source.cycle_step_hours
    return now.strftime("%Y%m%d"), cycle


# ---------------------------------------------------------------------------
# Byte-range download via .idx (unchanged from previous worker)


def _urlopen_with_retries(req, *, timeout: int, max_attempts: int = 3):
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
    with _urlopen_with_retries(urllib.request.Request(idx_url), timeout=30) as resp:
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
    with out.open("wb") as fh:
        for start, end in ranges:
            req = urllib.request.Request(grib_url)
            range_header = f"bytes={start}-{'' if end is None else end}"
            req.add_header("Range", range_header)
            with _urlopen_with_retries(req, timeout=120) as resp:
                fh.write(resp.read())


# ---------------------------------------------------------------------------
# Clipping + serialization


def clip_and_serialize(grid, bbox: tuple[float, float, float, float]) -> dict:
    import numpy as np
    min_lat, max_lat, min_lon, max_lon = bbox
    lat_mask = (grid.lats >= min_lat) & (grid.lats <= max_lat)
    lon_mask = (grid.lons >= min_lon) & (grid.lons <= max_lon)
    if not lat_mask.any() or not lon_mask.any():
        raise ValueError(
            f"empty grid after clip to bbox={bbox}; "
            f"grid lat=[{grid.lats.min():.2f},{grid.lats.max():.2f}] "
            f"lon=[{grid.lons.min():.2f},{grid.lons.max():.2f}]"
        )
    lats = grid.lats[lat_mask]
    lons = grid.lons[lon_mask]
    u = grid.u[np.ix_(lat_mask, lon_mask)]
    v = grid.v[np.ix_(lat_mask, lon_mask)]

    return {
        "source": grid.source,
        "reference_time": grid.reference_time.isoformat(),
        "valid_time": grid.valid_time.isoformat(),
        "bbox": {"min_lat": min_lat, "max_lat": max_lat,
                 "min_lon": min_lon, "max_lon": max_lon},
        "shape": [len(lats), len(lons)],
        "lats": lats.tolist(),
        "lons": lons.tolist(),
        "u": u.tolist(),
        "v": v.tolist(),
    }


# ---------------------------------------------------------------------------
# Redis + GCS writes


def _redis_client() -> redis.Redis:
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=0,
    )


def _write_redis(client: redis.Redis, key: str, ttl: int, blob: bytes) -> None:
    client.setex(key, ttl, blob)


def _write_gcs(source_name: str, region_name: str, cycle_iso: str,
               fhour: int, blob: bytes) -> str:
    bucket_name = os.environ["GCS_WEATHER_BUCKET"]
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    archive_path = f"{source_name}/{region_name}/{cycle_iso}/f{fhour:03d}.json.gz"
    obj = bucket.blob(archive_path)
    obj.content_encoding = "gzip"
    obj.upload_from_string(blob, content_type="application/json")

    # Per-cycle latest pointer for the default fhour preserves prior behaviour.
    return f"gs://{bucket_name}/{archive_path}"


# ---------------------------------------------------------------------------
# Core ingest — single fhour (kept compatible with existing tests)


def ingest(
    source_name: str,
    region_name: str,
    fhour: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Ingest a single fhour. Existing test contract preserved."""
    source, region = _resolve(source_name, region_name)
    if fhour is None:
        fhour = source.default_fhour

    payload, payload_gz, cycle_iso = _fetch_one(source, region, fhour)

    if dry_run:
        out_dir = Path(__file__).parent.parent / "ingest_output"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / f"{source.name}_{region.name}_f{fhour:03d}.json.gz"
        out_path.write_bytes(payload_gz)
        print(f"[{source.name}/{region.name}] dry-run -> {out_path}", flush=True)
        return payload

    client = _redis_client()
    fhour_key = f"weather:{source.name}:{region.name}:{cycle_iso}:f{fhour:03d}"
    _write_redis(client, fhour_key, source.cache_ttl_seconds, payload_gz)
    _write_gcs(source.name, region.name, cycle_iso, fhour, payload_gz)

    # Backwards-compat alias, only when this is the default fhour.
    if fhour == source.default_fhour:
        latest_key = f"weather:{source.name}:{region.name}:latest"
        _write_redis(client, latest_key, source.cache_ttl_seconds, payload_gz)

    return payload


def ingest_cycle(
    source_name: str,
    region_name: str,
    dry_run: bool = False,
) -> dict:
    """Ingest the FULL forecast sequence for the latest cycle.

    This is the new entry point. One Cloud Run Job invocation per cycle
    walks every fhour, writes a per-fhour Redis key, and finalises with
    a manifest + cycles-index update so the API can find this cycle.
    """
    source, region = _resolve(source_name, region_name)
    fhours = source.fhour_range()

    print(f"[{source.name}/{region.name}] cycle ingest, fhours={fhours[0]}..{fhours[-1]} "
          f"step={source.fhour_step} ({len(fhours)} files)", flush=True)

    cycle_iso: str | None = None
    valid_times: dict[int, str] = {}
    fhour_blobs: dict[int, bytes] = {}  # only for dry-run summary

    for fh in fhours:
        try:
            payload, payload_gz, this_cycle_iso = _fetch_one(source, region, fh)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  fhour {fh:03d}: 404 (not yet published) — stopping cycle", flush=True)
                break
            raise
        cycle_iso = this_cycle_iso  # consistent across the cycle
        valid_times[fh] = payload["valid_time"]

        if dry_run:
            fhour_blobs[fh] = payload_gz
            continue

        client = _redis_client()
        fhour_key = f"weather:{source.name}:{region.name}:{cycle_iso}:f{fh:03d}"
        _write_redis(client, fhour_key, source.cache_ttl_seconds, payload_gz)
        _write_gcs(source.name, region.name, cycle_iso, fh, payload_gz)

        if fh == source.default_fhour:
            latest_key = f"weather:{source.name}:{region.name}:latest"
            _write_redis(client, latest_key, source.cache_ttl_seconds, payload_gz)

    if cycle_iso is None:
        raise RuntimeError("no fhours ingested — even F00 failed")

    manifest = {
        "source": source.name,
        "region": region.name,
        "cycle": cycle_iso,
        "reference_time": valid_times.get(fhours[0]),  # F00 valid_time == cycle ref
        "fhours": sorted(valid_times.keys()),
        "valid_times": [valid_times[fh] for fh in sorted(valid_times.keys())],
    }
    manifest_blob = json.dumps(manifest, separators=(",", ":")).encode("utf-8")

    if dry_run:
        out_dir = Path(__file__).parent.parent / "ingest_output"
        out_dir.mkdir(exist_ok=True)
        (out_dir / f"{source.name}_{region.name}_{cycle_iso}_manifest.json").write_bytes(manifest_blob)
        print(f"[{source.name}/{region.name}] dry-run cycle complete: "
              f"{len(fhour_blobs)} files, manifest written", flush=True)
        return manifest

    client = _redis_client()
    manifest_key = f"weather:{source.name}:{region.name}:{cycle_iso}:manifest"
    _write_redis(client, manifest_key, source.cache_ttl_seconds, manifest_blob)

    # Cycles index — sorted set, score = cycle epoch, member = cycle iso.
    cycle_dt = datetime.strptime(cycle_iso, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    cycles_key = f"weather:{source.name}:{region.name}:cycles"
    client.zadd(cycles_key, {cycle_iso: cycle_dt.timestamp()})
    # Trim to the most recent 8 cycles per source — older cycles' keys age out
    # via TTL anyway. ZREMRANGEBYRANK keeps memory bounded.
    client.zremrangebyrank(cycles_key, 0, -9)

    print(f"[{source.name}/{region.name}] cycle ingest complete: "
          f"{len(valid_times)} fhours, cycle={cycle_iso}", flush=True)
    return manifest


def _resolve(source_name: str, region_name: str) -> tuple[Source, Region]:
    if region_name not in REGIONS:
        raise ValueError(f"unknown region: {region_name}. valid: {sorted(REGIONS)}")
    region = REGIONS[region_name]
    if source_name not in region.sources:
        raise ValueError(
            f"source {source_name!r} not configured for region {region_name!r}. "
            f"valid: {list(region.sources)}"
        )
    return SOURCES[source_name], region


def _fetch_one(source: Source, region: Region, fhour: int) -> tuple[dict, bytes, str]:
    """Download + parse + serialize one fhour. Returns (payload, gzip blob, cycle_iso)."""
    bbox = region.bbox
    target_resolution = region.resolution_for(source.name)
    date, cycle = latest_cycle(source)
    grib_url = source.url_fn(date, cycle, fhour)
    tag = f"{source.name}/{region.name}@{target_resolution}° f{fhour:03d}"
    print(f"[{tag}] cycle={date} {cycle:02d}Z", flush=True)

    ranges = fetch_ranges(f"{grib_url}.idx", WIND_FIELDS)
    if not ranges:
        raise RuntimeError(f"No matching wind fields in {grib_url}.idx")

    fd, tmp_path_str = tempfile.mkstemp(suffix=".grib2")
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    try:
        download_grib(grib_url, ranges, tmp_path)
        grid = parse_grib_to_wind_grid(
            tmp_path,
            source=source.name,
            target_bbox=bbox,
            target_resolution_deg=target_resolution,
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    payload = clip_and_serialize(grid, bbox)
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_gz = gzip.compress(payload_json)
    cycle_iso = grid.reference_time.strftime("%Y%m%dT%H%MZ")
    return payload, payload_gz, cycle_iso


def main() -> None:
    parser = argparse.ArgumentParser(description="NOAA weather ingestion worker")
    parser.add_argument("source", choices=sorted(SOURCES.keys()))
    parser.add_argument("--region", required=True, choices=sorted(REGIONS.keys()))
    parser.add_argument("--fhour", type=int,
                        help="Single-fhour mode (default: full cycle ingest)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write JSON to ./ingest_output/ instead of Redis/GCS")
    args = parser.parse_args()

    if args.fhour is not None:
        ingest(args.source, region_name=args.region, fhour=args.fhour, dry_run=args.dry_run)
    else:
        ingest_cycle(args.source, region_name=args.region, dry_run=args.dry_run)


if __name__ == "__main__":
    main()