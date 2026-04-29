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

HRRR_FIXTURE = Path(__file__).parent / "fixtures" / "hrrr_10m_wind_sample.grib2"
GREAT_LAKES_BBOX = (40.0, 50.0, -94.0, -75.0)


@pytest.fixture(scope="module")
def hrrr_grid() -> WindGrid:
    if not HRRR_FIXTURE.exists():
        pytest.skip(
            f"HRRR fixture missing: {HRRR_FIXTURE}. "
            f"Run `python scripts/download_fixture.py`."
        )
    return parse_grib_to_wind_grid(
        HRRR_FIXTURE, source="hrrr", target_bbox=GREAT_LAKES_BBOX
    )


def test_hrrr_regrids_to_1d(hrrr_grid: WindGrid):
    assert hrrr_grid.lats.ndim == 1 and hrrr_grid.lons.ndim == 1


def test_hrrr_covers_bbox(hrrr_grid: WindGrid):
    assert hrrr_grid.lats[0] >= 40.0 - 0.1
    assert hrrr_grid.lats[-1] <= 50.0 + 0.1
    assert hrrr_grid.lons[0] >= -94.0 - 0.1
    assert hrrr_grid.lons[-1] <= -75.0 + 0.1


def test_hrrr_wind_is_physical(hrrr_grid: WindGrid):
    assert np.isfinite(hrrr_grid.u).all() and np.isfinite(hrrr_grid.v).all()
    speed = np.sqrt(hrrr_grid.u ** 2 + hrrr_grid.v ** 2)
    assert speed.max() < 100, "regridding produced unphysical wind"
    assert 0.5 < speed.mean() < 25