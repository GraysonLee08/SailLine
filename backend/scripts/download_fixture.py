"""Download small GRIB2 fixtures for tests.

Reuses byte-range logic from workers.weather_ingest. Pulls both GFS (1°
regular grid) and HRRR (curvilinear) so tests cover both code paths.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from backend/
sys.path.insert(0, str(Path(__file__).parent.parent))

from workers.weather_ingest import (
    SOURCES,
    WIND_FIELDS,
    download_grib,
    fetch_ranges,
    latest_cycle,
)

FIXTURE_DIR = Path(__file__).parent.parent / "tests" / "fixtures"
GFS_FIXTURE = FIXTURE_DIR / "gfs_10m_wind_sample.grib2"
HRRR_FIXTURE = FIXTURE_DIR / "hrrr_10m_wind_sample.grib2"


def gfs_1deg_url(date: str, cycle: int, fhour: int) -> str:
    """1° GFS — ~1.5MB per file vs ~500MB for 0.25°. Ideal for fixtures."""
    return (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
        f"gfs.{date}/{cycle:02d}/atmos/gfs.t{cycle:02d}z.pgrb2.1p00.f{fhour:03d}"
    )


def hrrr_fixture_url(date: str, cycle: int, fhour: int) -> str:
    return (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/"
        f"hrrr.{date}/conus/hrrr.t{cycle:02d}z.wrfsfcf{fhour:02d}.grib2"
    )


def main() -> None:
    # GFS 1° fixture
    date, cycle = latest_cycle(SOURCES["gfs"])
    url = gfs_1deg_url(date, cycle, fhour=6)
    print(f"GFS index: {url}.idx")
    ranges = fetch_ranges(f"{url}.idx", WIND_FIELDS)
    if not ranges:
        sys.exit("No matching GFS fields in .idx — NOAA may have rotated the run.")
    print(f"  Downloading {len(ranges)} field(s) -> {GFS_FIXTURE}")
    download_grib(url, ranges, GFS_FIXTURE)
    print(f"  Done: {GFS_FIXTURE.stat().st_size / 1024:.1f} KB")

    # HRRR fixture (curvilinear; tests the regridding path)
    h_date, h_cycle = latest_cycle(SOURCES["hrrr"])
    h_url = hrrr_fixture_url(h_date, h_cycle, fhour=1)
    print(f"\nHRRR index: {h_url}.idx")
    h_ranges = fetch_ranges(f"{h_url}.idx", WIND_FIELDS)
    if not h_ranges:
        sys.exit("No matching HRRR fields in .idx — NOAA may have rotated the run.")
    print(f"  Downloading {len(h_ranges)} field(s) -> {HRRR_FIXTURE}")
    download_grib(h_url, h_ranges, HRRR_FIXTURE)
    print(f"  Done: {HRRR_FIXTURE.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()