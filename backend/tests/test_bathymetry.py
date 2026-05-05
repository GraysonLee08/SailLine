"""Tests for app.services.bathymetry."""
from __future__ import annotations

import io
from unittest.mock import patch

import numpy as np
import pytest

from app.services import bathymetry
from app.services.bathymetry import DepthGrid, BathymetryUnavailable


@pytest.fixture(autouse=True)
def clear_cache():
    bathymetry.invalidate_cache()
    yield
    bathymetry.invalidate_cache()


def _synthetic_grid() -> DepthGrid:
    lats = np.array([41.5, 42.0, 42.5, 43.0])
    lons = np.array([-88.0, -87.5, -87.0])
    # Depth ramps from 5 m at lon=-88 to 100 m at lon=-87
    depth = np.array([
        [5.0, 50.0, 100.0],
        [5.0, 50.0, 100.0],
        [5.0, 50.0, 100.0],
        [5.0, 50.0, 100.0],
    ], dtype=np.float32)
    return DepthGrid(
        lats=lats, lons=lons, depth_m=depth,
        region="conus", source="synthetic", datum="LWD",
    )


def test_depth_grid_sample_interior():
    g = _synthetic_grid()
    # Halfway between -88 and -87.5 in lon → midway between depth 5 and 50
    d = g.sample(42.0, -87.75)
    assert abs(d - 27.5) < 0.001


def test_depth_grid_sample_outside_returns_none():
    g = _synthetic_grid()
    assert g.sample(50.0, -87.5) is None
    assert g.sample(42.0, -100.0) is None


def test_depth_grid_nan_propagates():
    g = _synthetic_grid()
    g.depth_m[1, 1] = float("nan")
    d = g.sample(42.0, -87.5)  # exactly on the NaN cell
    assert np.isnan(d)


def test_for_region_caches():
    grid = _synthetic_grid()
    with patch.object(bathymetry, "_load_from_gcs", return_value=grid) as loader:
        a = bathymetry.for_region("conus")
        b = bathymetry.for_region("conus")
    assert a is b
    assert loader.call_count == 1


def test_for_region_raises_when_missing():
    with patch.object(bathymetry, "_load_from_gcs", return_value=None):
        with pytest.raises(BathymetryUnavailable):
            bathymetry.for_region("never_ingested")


def test_for_region_caches_negative_result():
    """Once we know a region isn't ingested, don't keep hitting GCS."""
    with patch.object(bathymetry, "_load_from_gcs", return_value=None) as loader:
        with pytest.raises(BathymetryUnavailable):
            bathymetry.for_region("never_ingested")
        with pytest.raises(BathymetryUnavailable):
            bathymetry.for_region("never_ingested")
    assert loader.call_count == 1


def test_npz_round_trip():
    """The .npz format the worker produces unpacks correctly."""
    lats = np.array([41.0, 42.0, 43.0], dtype=np.float64)
    lons = np.array([-88.0, -87.0], dtype=np.float64)
    depth = np.array([[10.0, 20.0], [30.0, 40.0], [50.0, 60.0]], dtype=np.float32)

    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        lats=lats, lons=lons, depth_m=depth,
        source=np.array("ncei_great_lakes"),
        datum=np.array("LWD"),
    )
    buf.seek(0)

    with np.load(buf) as data:
        assert np.array_equal(data["lats"], lats)
        assert np.array_equal(data["lons"], lons)
        assert np.array_equal(data["depth_m"], depth)
        assert str(data["source"]) == "ncei_great_lakes"
        assert str(data["datum"]) == "LWD"
