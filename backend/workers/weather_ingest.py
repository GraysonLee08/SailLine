"""NOAA weather ingestion -> wind grid JSON, clipped to a region bbox.

Production: runs as a Cloud Run Job per (source, region). One invocation
ingests the full forecast sequence for the latest cycle (HRRR F00-F18,
GFS F000-F120 at 3h step) and writes per-fhour keys to Redis + GCS.
Local: --dry-run writes JSON to ./ingest_output/ instead.

After Phase 4 of the architecture review, the per-cycle orchestration
(fhour walk → Redis → GCS → manifest → cycles index) lives in
``app.services.ingest.cycle_pipeline.CyclicalIngestPipeline``. This
module keeps the source registry, the .grib2 byte-range download, the
GRIB → JSON serialisation, and a thin :class:`WeatherSnapshotSource`
adapter — about half the LOC it carried before.

Usage (from backend/):
    python -m workers.weather_ingest hrrr --region conus --dry-run
    python -m workers.weather_ingest gfs --region conus --dry-run
    python -m workers.weather_ingest hrrr --region conus --fhour 1 --dry-run

Redis key shape (preserved exactly across the refactor):
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import redis
from google.cloud import storage

from app.regions import REGIONS, Region
from app.services.grib import parse_grib_to_wind_grid
from app.services.ingest import (
    CyclicalIngestPipeline,
    GcsArchive,
    SnapshotResult,
)
from app.services.ingest.archive import Archive
from app.services.redis_keys import (
    weather_cycles_index_key,
    weather_fhour_key,
    weather_latest_alias_key,
    weather_manifest_key,
)

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


# Trim the cycles sorted set to this many entries. Older cycles' keys
# age out via TTL anyway — this just keeps the index bounded.
CYCLES_TRIM_KEEP = 8


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
# Fetch one fhour — used by both single-fhour ``ingest()`` and the pipeline


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


# ---------------------------------------------------------------------------
# SnapshotSource adapter


@dataclass
class WeatherSnapshotSource:
    """Adapter that plugs a (source, region) pair into the cycle pipeline.

    Holds the source config + region. Implements the
    :class:`~app.services.ingest.SnapshotSource` Protocol so the
    pipeline can drive a full-cycle ingest without knowing about
    GRIB or NOMADS.
    """
    source: Source
    region: Region

    @property
    def name(self) -> str:
        return f"{self.source.name}/{self.region.name}"

    @property
    def cycle_ttl_seconds(self) -> int:
        return self.source.cache_ttl_seconds

    @property
    def cycles_trim_keep(self) -> int:
        return CYCLES_TRIM_KEEP

    def fhour_range(self) -> list[int]:
        return self.source.fhour_range()

    def fetch_snapshot(self, fhour: int) -> SnapshotResult:
        payload, payload_gz, cycle_iso = _fetch_one(self.source, self.region, fhour)
        return SnapshotResult(
            blob_gz=payload_gz,
            cycle_iso=cycle_iso,
            valid_time_iso=payload["valid_time"],
            extras=None,
        )

    def snapshot_redis_key(self, cycle_iso: str, fhour: int) -> str:
        return weather_fhour_key(self.source.name, self.region.name, cycle_iso, fhour)

    def manifest_redis_key(self, cycle_iso: str) -> str:
        return weather_manifest_key(self.source.name, self.region.name, cycle_iso)

    def cycles_index_redis_key(self) -> str:
        return weather_cycles_index_key(self.source.name, self.region.name)

    def snapshot_archive_path(self, cycle_iso: str, fhour: int) -> str:
        return f"{self.source.name}/{self.region.name}/{cycle_iso}/f{fhour:03d}.json.gz"

    def aliases_for_fhour(self, cycle_iso: str, fhour: int) -> list[str]:
        """Maintain the backwards-compat ``:latest`` pointer.

        Only the source's ``default_fhour`` mirrors to ``:latest`` —
        otherwise the wind overlay would flicker between fhours as the
        cycle filled in.
        """
        if fhour == self.source.default_fhour:
            return [weather_latest_alias_key(self.source.name, self.region.name)]
        return []

    def persist_first_snapshot_extras(
        self, *, redis_client, archive: Optional[Archive], snapshot: SnapshotResult,
    ) -> None:
        """No-op for weather. The :latest alias handling lives in
        :meth:`aliases_for_fhour` because it's per-fhour, not once-per-cycle.
        """
        return

    def build_manifest_fields(
        self, *, cycle_iso: str, fhours: list[int], valid_times: list[str],
    ) -> dict:
        return {
            "source": self.source.name,
            "region": self.region.name,
            "cycle": cycle_iso,
            "reference_time": valid_times[0] if valid_times else None,  # F00 == cycle ref
            "fhours": fhours,
            "valid_times": valid_times,
        }

    def dry_run_snapshot_filename(self, cycle_iso: str, fhour: int) -> str:
        return f"{self.source.name}_{self.region.name}_f{fhour:03d}.json.gz"

    def dry_run_manifest_filename(self, cycle_iso: str) -> str:
        return f"{self.source.name}_{self.region.name}_{cycle_iso}_manifest.json"


# ---------------------------------------------------------------------------
# Resolve / Redis / archive plumbing


def _resolve(source_name: str, region_name: str) -> tuple[Source, Region]:
    if region_name not in REGIONS:
        raise ValueError(f"unknown region: {region_name}. valid: {sorted(REGIONS)}")
    region = REGIONS[region_name]
    if source_name not in region.sources:
        raise ValueError(
            f"source {source_name!r} not configured for region {region_name!r}. "
            f"valid: {sorted(region.sources)}"
        )
    if source_name not in SOURCES:
        raise ValueError(f"unknown source: {source_name}. valid: {sorted(SOURCES)}")
    return SOURCES[source_name], region


def _redis_client() -> redis.Redis:
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=0,
    )


def _make_archive() -> GcsArchive:
    """Wire a GcsArchive against the weather bucket. Tests patch
    ``workers.weather_ingest.storage.Client`` so the existing
    storage-mock chain still works."""
    return GcsArchive.from_env("GCS_WEATHER_BUCKET", storage_module=storage)


# ---------------------------------------------------------------------------
# Public entrypoints


def ingest(
    source_name: str,
    region_name: str,
    fhour: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Single-fhour ingest. Existing test contract preserved.

    Used for ad-hoc backfills and the test surface that pins per-fhour
    write semantics (per-fhour key + alias only on default_fhour). The
    cycle-pipeline path is :func:`ingest_cycle`.
    """
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
    archive = _make_archive()
    fhour_key = weather_fhour_key(source.name, region.name, cycle_iso, fhour)
    client.setex(fhour_key, source.cache_ttl_seconds, payload_gz)
    archive.upload(
        f"{source.name}/{region.name}/{cycle_iso}/f{fhour:03d}.json.gz",
        payload_gz,
        content_type="application/json",
        gzip_encoded=True,
    )

    # Backwards-compat alias, only when this is the default fhour.
    if fhour == source.default_fhour:
        latest_key = weather_latest_alias_key(source.name, region.name)
        client.setex(latest_key, source.cache_ttl_seconds, payload_gz)

    return payload


def ingest_cycle(
    source_name: str,
    region_name: str,
    dry_run: bool = False,
) -> dict:
    """Ingest the FULL forecast sequence for the latest cycle.

    Delegates to :class:`CyclicalIngestPipeline`. The orchestration
    (fhour walk, 404 stop, manifest write, cycles index update) lives
    there; this function only chooses the source/region and wires the
    Redis client + archive.

    Returns the manifest dict so existing tests keep working.
    """
    source, region = _resolve(source_name, region_name)
    weather_source = WeatherSnapshotSource(source=source, region=region)

    if dry_run:
        dry_run_dir = Path(__file__).parent.parent / "ingest_output"
        dry_run_dir.mkdir(exist_ok=True)
        pipeline = CyclicalIngestPipeline(
            weather_source,
            dry_run=True,
            dry_run_dir=dry_run_dir,
        )
    else:
        pipeline = CyclicalIngestPipeline(
            weather_source,
            redis_client=_redis_client(),
            archive=_make_archive(),
        )

    manifest = pipeline.run_cycle()
    return manifest.fields


# ---------------------------------------------------------------------------
# CLI


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
