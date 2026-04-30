"""Live NOAA smoke test — gated behind RUN_REAL_NOAA_TESTS=1.

Run before deploys to catch breaking changes to NOAA's GRIB2 format,
URL scheme, or .idx structure. NOT run in CI (slow + flaky by design).

    $env:RUN_REAL_NOAA_TESTS=1     # PowerShell
    set RUN_REAL_NOAA_TESTS=1      # cmd
    export RUN_REAL_NOAA_TESTS=1   # mac/linux
    python -m pytest tests/test_weather_ingest_live.py -v

Expect ~30-60s per source (downloads real GRIB2 byte ranges from NOMADS).
Output written to backend/ingest_output/ (gitignored).
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from workers.weather_ingest import DEFAULT_BBOX, ingest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not os.environ.get("RUN_REAL_NOAA_TESTS"),
        reason="set RUN_REAL_NOAA_TESTS=1 to run real-NOAA smoke tests",
    ),
]


@pytest.mark.parametrize("source", ["gfs", "hrrr"])
def test_real_noaa_end_to_end(source: str):
    """Hit real NOAA, parse the freshest cycle, validate payload shape.

    Uses dry_run=True so Redis/GCS aren't touched. The payload still goes
    through the full fetch -> download -> parse -> clip -> serialize path —
    that's what we want to catch if NOAA breaks something."""
    payload = ingest(source, dry_run=True)

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

    # Bbox is the configured great_lakes default
    assert payload["bbox"]["min_lat"] == DEFAULT_BBOX[0]
    assert payload["bbox"]["max_lat"] == DEFAULT_BBOX[1]
    assert payload["bbox"]["min_lon"] == DEFAULT_BBOX[2]
    assert payload["bbox"]["max_lon"] == DEFAULT_BBOX[3]
