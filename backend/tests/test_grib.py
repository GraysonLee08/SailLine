"""Tests for app/services/grib.py against a small fixture GRIB2 file."""
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from app.services.grib import WindGrid, parse_grib_to_wind_grid

FIXTURE = Path(__file__).parent / "fixtures" / "gfs_10m_wind_sample.grib2"


@pytest.fixture(scope="module")
def grid() -> WindGrid:
    if not FIXTURE.exists():
        pytest.skip(
            f"Fixture missing: {FIXTURE}. Run `python scripts/download_fixture.py`."
        )
    return parse_grib_to_wind_grid(FIXTURE, source="gfs")


def test_shape(grid: WindGrid):
    assert grid.u.shape == (len(grid.lats), len(grid.lons))
    assert grid.v.shape == grid.u.shape


def test_lons_normalized(grid: WindGrid):
    assert -180 <= grid.lons.min() and grid.lons.max() <= 180
    assert np.all(np.diff(grid.lons) > 0), "lons must be sorted ascending"


def test_lats_in_range(grid: WindGrid):
    assert -90 <= grid.lats.min() and grid.lats.max() <= 90


def test_wind_is_physical(grid: WindGrid):
    """10m winds rarely exceed 70 m/s; >100 indicates a unit/parse bug."""
    assert np.isfinite(grid.u).all() and np.isfinite(grid.v).all()
    assert np.abs(grid.u).max() < 100
    assert np.abs(grid.v).max() < 100
    speed = np.sqrt(grid.u ** 2 + grid.v ** 2)
    assert 1 < speed.mean() < 20, f"mean wind {speed.mean():.2f} m/s looks wrong"


def test_times(grid: WindGrid):
    assert isinstance(grid.reference_time, datetime)
    assert grid.valid_time >= grid.reference_time


def test_lake_michigan_has_wind(grid: WindGrid):
    """Sanity: a point in the middle of Lake Michigan should have a finite wind."""
    i = np.argmin(np.abs(grid.lats - 43.5))
    j = np.argmin(np.abs(grid.lons - (-87.0)))
    assert np.isfinite(grid.u[i, j]) and np.isfinite(grid.v[i, j])