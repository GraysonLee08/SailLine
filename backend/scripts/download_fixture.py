"""Download a small GFS fixture for tests.

Grabs only UGRD + VGRD at 10m from the latest available 1-degree GFS run
using HTTP Range requests against the .idx file. Result is ~500KB.
"""
from __future__ import annotations

import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIELDS = (":UGRD:10 m above ground:", ":VGRD:10 m above ground:")
FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "gfs_10m_wind_sample.grib2"


def latest_cycle() -> tuple[str, str]:
    """GFS runs at 00/06/12/18 UTC; back off 6h so the run is fully published."""
    now = datetime.now(timezone.utc) - timedelta(hours=6)
    cycle = (now.hour // 6) * 6
    return now.strftime("%Y%m%d"), f"{cycle:02d}"


def fetch_ranges(idx_url: str, fields: tuple[str, ...]) -> list[tuple[int, int | None]]:
    """Parse a .idx file. Each line: idx:byte_offset:date:field:level:forecast:."""
    with urllib.request.urlopen(idx_url, timeout=30) as resp:
        lines = [ln for ln in resp.read().decode("ascii").splitlines() if ln.strip()]

    entries = []
    for ln in lines:
        parts = ln.split(":", 2)
        entries.append((int(parts[0]), int(parts[1]), ":" + parts[2]))

    ranges: list[tuple[int, int | None]] = []
    for i, (_, offset, descriptor) in enumerate(entries):
        if any(f in descriptor for f in fields):
            end = entries[i + 1][1] - 1 if i + 1 < len(entries) else None
            ranges.append((offset, end))
    return ranges


def download_ranges(grib_url: str, ranges: list[tuple[int, int | None]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        for start, end in ranges:
            header = f"bytes={start}-{end if end is not None else ''}"
            req = urllib.request.Request(grib_url, headers={"Range": header})
            with urllib.request.urlopen(req, timeout=60) as resp:
                f.write(resp.read())


def main() -> None:
    date, cycle = latest_cycle()
    base = (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
        f"gfs.{date}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.1p00.f006"
    )
    print(f"Index: {base}.idx")
    ranges = fetch_ranges(f"{base}.idx", FIELDS)
    if not ranges:
        sys.exit("No matching fields in .idx — try again, NOAA may have rotated the file.")

    print(f"Downloading {len(ranges)} field(s) -> {FIXTURE}")
    download_ranges(base, ranges, FIXTURE)
    print(f"Done: {FIXTURE.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()