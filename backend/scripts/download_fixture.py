"""Download a small GFS 1° fixture for tests.

Reuses byte-range logic from workers.weather_ingest.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from backend/
sys.path.insert(0, str(Path(__file__).parent.parent))

from workers.weather_ingest import (
    SOURCES, WIND_FIELDS, download_grib, fetch_ranges, latest_cycle,
)

FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "gfs_10m_wind_sample.grib2"


def gfs_1deg_url(date: str, cycle: int, fhour: int) -> str:
    """1° GFS — much smaller than 0.25° (~1.5MB vs ~500MB) — perfect for fixtures."""
    return (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
        f"gfs.{date}/{cycle:02d}/atmos/gfs.t{cycle:02d}z.pgrb2.1p00.f{fhour:03d}"
    )


def main() -> None:
    date, cycle = latest_cycle(SOURCES["gfs"])
    url = gfs_1deg_url(date, cycle, fhour=6)
    print(f"Index: {url}.idx")
    ranges = fetch_ranges(f"{url}.idx", WIND_FIELDS)
    if not ranges:
        sys.exit("No matching fields in .idx — NOAA may have rotated the run.")
    print(f"Downloading {len(ranges)} field(s) -> {FIXTURE}")
    download_grib(url, ranges, FIXTURE)
    print(f"Done: {FIXTURE.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()