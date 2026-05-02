"""Live NOAA smoke test — gated behind RUN_REAL_NOAA_TESTS=1.

Run before deploys to catch breaking changes to NOAA's GRIB2 format,
URL scheme, or .idx structure. NOT run in CI (slow + flaky by design).

    $env:RUN_REAL_NOAA_TESTS=1     # PowerShell
    set RUN_REAL_NOAA_TESTS=1      # cmd
    export RUN_REAL_NOAA_TESTS=1   # mac/linux
    python -m pytest tests/test_weather_ingest_live.py -v

Expect ~30-60s per source (downloads real GRIB2 byte ranges from NOMADS).
HRRR conus is significantly slower (~3-5 min) because the regridding pass
operates on HRRR's full ~1.9M-point native LCC grid. Output written to
backend/ingest_output/ (gitignored).
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from app.regions import REGIONS
from workers.weather_ingest import ingest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not os.environ.get("RUN_REAL_NOAA_TESTS"),
        reason="set RUN_REAL_NOAA_TESTS=1 to run real-NOAA smoke tests",
    ),
]


@pytest.mark.parametrize("source", ["gfs", "hrrr"])
def test_real_noaa_conus_end_to_end(source: str):
    """Hit real NOAA, parse the freshest cycle, validate payload shape.

    Uses dry_run=True so Redis/GCS aren't touched. The payload still goes
    through the full fetch -> download -> parse -> clip -> serialize path —
    that's what we want to catch if NOAA breaks something."""
    payload = ingest(source, region_name="conus", dry_run=True)

    # Schema sanity — guards against silent shape drift
    assert set(payload) == {
        "source", "reference_time", "valid_time", "bbox",
        "shape", "lats", "lons", "u", "v",
    }
    assert payload["source"] == source
    assert payload["shape"] == [len(payload["lats"]), len(payload["lons"])]

    # Plausible wind values — catches unit/parse breakage
    u = np.array(payload["u"])
    v = np.array(payload["v"])
    assert np.isfinite(u).all() and np.isfinite(v).all()
    speed = np.sqrt(u ** 2 + v ** 2)
    assert speed.max() < 100, f"max wind {speed.max()} m/s — unphysical, parse broke?"
    assert 0.5 < speed.mean() < 25, f"mean wind {speed.mean()} m/s looks wrong"

    # Bbox matches the conus registry entry
    expected = REGIONS["conus"].bbox
    assert payload["bbox"]["min_lat"] == expected[0]
    assert payload["bbox"]["max_lat"] == expected[1]
    assert payload["bbox"]["min_lon"] == expected[2]
    assert payload["bbox"]["max_lon"] == expected[3]


def test_real_noaa_venue_native_resolution():
    """Venue HRRR runs at native 0.027° (~3 km). Verifies the high-res path
    actually produces a denser grid than CONUS HRRR would for the same area."""
    payload = ingest("hrrr", region_name="sf_bay", dry_run=True)

    # sf_bay bbox is 0.8° lat × 0.7° lon. At 0.027° step, we should see
    # roughly 30×26 = ~800 grid cells. Anything much smaller means the
    # resolution arg didn't propagate.
    rows, cols = payload["shape"]
    assert rows >= 25, f"too few lat rows ({rows}) — resolution may not have propagated"
    assert cols >= 20, f"too few lon cols ({cols}) — resolution may not have propagated"
