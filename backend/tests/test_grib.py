"""Tests for app/services/grib.py.

Fixture-gated tests round-trip real GFS/HRRR GRIB2 samples. The
synthetic-grid tests at the bottom pin the cached-regridder behaviour
(equivalence with scipy.griddata, cache reuse, nearest fill outside the
hull) and always run — no fixture download needed.
"""
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

import app.services.grib as grib
from app.services.grib import WindGrid, _regrid_curvilinear, parse_grib_to_wind_grid

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


# ─── Cached regridder (synthetic curvilinear grid — no fixture needed) ───


def _synthetic_curvilinear(n: int = 30):
    """Gently sheared lat/lon mesh — genuinely curvilinear like HRRR's
    LCC grid, small enough that Delaunay is instant."""
    x = np.linspace(-90.0, -82.0, n)
    y = np.linspace(41.0, 47.0, n)
    lon2d, lat2d = np.meshgrid(x, y)
    lon2d = lon2d + 0.05 * (lat2d - 44.0)
    lat2d = lat2d + 0.03 * (lon2d + 86.0)
    u = (np.sin(lon2d / 3.0) * 5 + 2).astype(np.float32)
    v = (np.cos(lat2d / 2.0) * 4 - 1).astype(np.float32)
    return lat2d, lon2d, u, v


INNER_BBOX = (42.0, 46.0, -89.0, -83.0)   # fully inside the synthetic grid
WIDE_BBOX = (40.0, 48.0, -92.0, -80.0)    # extends past it → nearest fill


@pytest.fixture(autouse=True)
def _clear_regridder_cache():
    grib._REGRIDDER_CACHE.clear()
    yield
    grib._REGRIDDER_CACHE.clear()


def _griddata_reference(lat2d, lon2d, values, bbox, resolution):
    """The exact computation the pre-cache implementation performed —
    used as the equivalence oracle for the precomputed-weights path."""
    from scipy.interpolate import griddata

    min_lat, max_lat, min_lon, max_lon = bbox
    buffer = 0.5
    in_bbox = (
        (lat2d >= min_lat - buffer) & (lat2d <= max_lat + buffer)
        & (lon2d >= min_lon - buffer) & (lon2d <= max_lon + buffer)
    )
    pts = np.column_stack([lon2d[in_bbox], lat2d[in_bbox]])
    vals = values[in_bbox]
    tgt_lats = np.arange(min_lat, max_lat + resolution / 2, resolution)
    tgt_lons = np.arange(min_lon, max_lon + resolution / 2, resolution)
    lon_mesh, lat_mesh = np.meshgrid(tgt_lons, tgt_lats)
    tgt_pts = np.column_stack([lon_mesh.ravel(), lat_mesh.ravel()])
    out = griddata(pts, vals, tgt_pts, method="linear")
    if np.isnan(out).any():
        out = np.where(
            np.isnan(out), griddata(pts, vals, tgt_pts, method="nearest"), out,
        )
    return out.reshape(lat_mesh.shape).astype(np.float32)


def test_regrid_matches_griddata_inside_hull():
    """Precomputed-weights output must equal what scipy.griddata produced —
    same triangulation, same barycentric interpolation."""
    lat2d, lon2d, u, v = _synthetic_curvilinear()
    tgt_lats, tgt_lons, u_out, v_out = _regrid_curvilinear(
        lat2d, lon2d, u, v, INNER_BBOX, 0.25,
    )
    u_ref = _griddata_reference(lat2d, lon2d, u, INNER_BBOX, 0.25)
    v_ref = _griddata_reference(lat2d, lon2d, v, INNER_BBOX, 0.25)

    np.testing.assert_allclose(u_out, u_ref, atol=1e-4)
    np.testing.assert_allclose(v_out, v_ref, atol=1e-4)
    assert u_out.shape == (len(tgt_lats), len(tgt_lons))


def test_regrid_matches_griddata_with_nearest_fill():
    """Target bbox extending past the source grid exercises the
    outside-convex-hull nearest-neighbour path; output must stay finite
    and equal the griddata reference."""
    lat2d, lon2d, u, v = _synthetic_curvilinear()
    _, _, u_out, v_out = _regrid_curvilinear(
        lat2d, lon2d, u, v, WIDE_BBOX, 0.25,
    )
    u_ref = _griddata_reference(lat2d, lon2d, u, WIDE_BBOX, 0.25)

    assert np.isfinite(u_out).all() and np.isfinite(v_out).all()
    np.testing.assert_allclose(u_out, u_ref, atol=1e-4)


def test_regridder_built_once_for_repeated_geometry():
    """The whole point: 19 fhours share one geometry build. Same grid +
    bbox + resolution must hit the cache, not re-triangulate."""
    lat2d, lon2d, u, v = _synthetic_curvilinear()

    with patch.object(grib, "_build_regridder", wraps=grib._build_regridder) as mock_build:
        _regrid_curvilinear(lat2d, lon2d, u, v, INNER_BBOX, 0.25)
        _regrid_curvilinear(lat2d, lon2d, u + 1.0, v - 1.0, INNER_BBOX, 0.25)
        _regrid_curvilinear(lat2d, lon2d, u * 2.0, v, INNER_BBOX, 0.25)

    assert mock_build.call_count == 1
    assert len(grib._REGRIDDER_CACHE) == 1


def test_regridder_cache_keys_on_bbox_and_resolution():
    """Different bbox or resolution = different target geometry = its own
    cache entry (venue vs conus regions must never share weights)."""
    lat2d, lon2d, u, v = _synthetic_curvilinear()

    _regrid_curvilinear(lat2d, lon2d, u, v, INNER_BBOX, 0.25)
    _regrid_curvilinear(lat2d, lon2d, u, v, INNER_BBOX, 0.10)
    _regrid_curvilinear(lat2d, lon2d, u, v, (43.0, 45.0, -88.0, -84.0), 0.25)

    assert len(grib._REGRIDDER_CACHE) == 3


def test_regrid_values_change_with_input_despite_cache():
    """Cache holds geometry only — fresh values must flow through."""
    lat2d, lon2d, u, v = _synthetic_curvilinear()
    _, _, u1, _ = _regrid_curvilinear(lat2d, lon2d, u, v, INNER_BBOX, 0.25)
    _, _, u2, _ = _regrid_curvilinear(lat2d, lon2d, u + 3.0, v, INNER_BBOX, 0.25)

    np.testing.assert_allclose(u2 - u1, 3.0, atol=1e-4)