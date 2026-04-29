"""NOAA weather ingestion -> wind grid JSON, clipped to a bbox.

Production: runs as a Cloud Run Job, writes to Redis + GCS.
Local: --dry-run flag writes JSON to ./ingest_output/ instead.

Usage (from backend/):
    python -m workers.weather_ingest gfs --dry-run
    python -m workers.weather_ingest hrrr --dry-run
    python -m workers.weather_ingest gfs --fhour 12 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from app.services.grib import WindGrid, parse_grib_to_wind_grid

# Great Lakes + buffer: min_lat, max_lat, min_lon, max_lon
DEFAULT_BBOX = (40.0, 50.0, -94.0, -75.0)

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
    cycle_step_hours: int     # GFS runs every 6h, HRRR every 1h
    publish_lag_hours: int    # min wait after cycle before files are stable
    default_fhour: int


SOURCES: dict[str, Source] = {
    "gfs": Source("gfs", gfs_url, cycle_step_hours=6, publish_lag_hours=5, default_fhour=6),
    "hrrr": Source("hrrr", hrrr_url, cycle_step_hours=1, publish_lag_hours=2, default_fhour=1),
}


def latest_cycle(source: Source) -> tuple[str, int]:
    """Pick the most recent run that should be fully published."""
    now = datetime.now(timezone.utc) - timedelta(hours=source.publish_lag_hours)
    cycle = (now.hour // source.cycle_step_hours) * source.cycle_step_hours
    return now.strftime("%Y%m%d"), cycle


# ---------------------------------------------------------------------------
# Byte-range download via .idx (shared with scripts/download_fixture.py)


def fetch_ranges(idx_url: str, fields: tuple[str, ...]) -> list[tuple[int, int | None]]:
    """Parse a NOAA .idx file, return (start, end) byte ranges for matching fields."""
    with urllib.request.urlopen(idx_url, timeout=30) as resp:
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
            with urllib.request.urlopen(req, timeout=120) as resp:
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


def ingest(source_name: str, fhour: int | None = None, dry_run: bool = False) -> dict:
    source = SOURCES[source_name]
    if fhour is None:
        fhour = source.default_fhour

    date, cycle = latest_cycle(source)
    grib_url = source.url_fn(date, cycle, fhour)
    print(f"[{source.name}] cycle={date} {cycle:02d}Z fhour={fhour:03d}", flush=True)
    print(f"[{source.name}] url={grib_url}", flush=True)

    ranges = fetch_ranges(f"{grib_url}.idx", WIND_FIELDS)
    if not ranges:
        raise RuntimeError(f"No matching wind fields in {grib_url}.idx")

    fd, tmp_path_str = tempfile.mkstemp(suffix=".grib2")
    os.close(fd)  # Windows won't let us unlink an open file
    tmp_path = Path(tmp_path_str)
    try:
        download_grib(grib_url, ranges, tmp_path)
        print(
            f"[{source.name}] downloaded {tmp_path.stat().st_size / 1024:.1f} KB",
            flush=True,
        )
        grid = parse_grib_to_wind_grid(tmp_path, source=source.name)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except PermissionError:
            # Defensive: on Windows, xarray/cfgrib can occasionally hold the file
            # briefly after close. The OS will clean Temp on its own; not worth dying.
            pass

    bbox_env = os.environ.get("WEATHER_BBOX")
    bbox = tuple(map(float, bbox_env.split(","))) if bbox_env else DEFAULT_BBOX
    payload = clip_and_serialize(grid, bbox)
    print(
        f"[{source.name}] clipped to {payload['shape'][0]}x{payload['shape'][1]} grid",
        flush=True,
    )

    if dry_run:
        out_dir = Path(__file__).parent.parent / "ingest_output"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / f"{source.name}_f{fhour:03d}.json"
        out_path.write_text(json.dumps(payload))
        print(
            f"[{source.name}] dry-run -> {out_path} "
            f"({out_path.stat().st_size / 1024:.1f} KB)",
            flush=True,
        )
    else:
        raise NotImplementedError(
            "Redis/GCS writes land in step 3. Use --dry-run for now."
        )

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="NOAA weather ingestion worker")
    parser.add_argument("source", choices=sorted(SOURCES.keys()))
    parser.add_argument("--fhour", type=int, help="Forecast hour (default: source-specific)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write JSON to ./ingest_output/ instead of Redis/GCS")
    args = parser.parse_args()
    ingest(args.source, fhour=args.fhour, dry_run=args.dry_run)


if __name__ == "__main__":
    main()