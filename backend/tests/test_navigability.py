"""Tests for app.services.routing.navigability."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest
from shapely.geometry import Polygon

from app.services import bathymetry, charts
from app.services.bathymetry import DepthGrid
from app.services.charts import HazardIndex
from app.services.routing.navigability import make_navigable_predicate
from shapely.strtree import STRtree


@pytest.fixture(autouse=True)
def clear_caches():
    bathymetry.invalidate_cache()
    charts.invalidate_cache()
    yield
    bathymetry.invalidate_cache()
    charts.invalidate_cache()


def _depth_grid_with_shore() -> DepthGrid:
    """Lat/lon grid where lon < -87.7 is land (negative depth) and lon >= -87.5 is water (50m)."""
    lats = np.array([41.5, 42.0, 42.5])
    lons = np.array([-88.0, -87.7, -87.5, -87.0])
    # row pattern: [land, shallow, deep, deep]
    row = np.array([-10.0, 2.0, 50.0, 50.0], dtype=np.float32)
    depth = np.tile(row, (3, 1))
    return DepthGrid(
        lats=lats, lons=lons, depth_m=depth,
        region="conus", source="synthetic", datum="LWD",
    )


def _hazard_index_with_one_polygon() -> HazardIndex:
    # Small polygon in deep water — the boat shouldn't sail through it.
    poly = Polygon([
        (-87.55, 42.05),
        (-87.45, 42.05),
        (-87.45, 42.15),
        (-87.55, 42.15),
    ])
    return HazardIndex(
        polygons=[poly], tree=STRtree([poly]),
        region="conus", source_layers=("OBSTRN",), feature_count=1,
    )


def test_depth_only_blocks_land():
    grid = _depth_grid_with_shore()
    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", return_value=None):
            is_navigable = make_navigable_predicate("conus", draft_m=2.0)

    # Deep water: navigable
    assert is_navigable(42.0, -87.5) is True
    # Land: blocked
    assert is_navigable(42.0, -88.0) is False
    # Shallow (2m depth, draft 2m × 1.5 = 3m needed): blocked
    assert is_navigable(42.0, -87.7) is False


def test_outside_grid_fails_open():
    """Engine is bounded by the wind grid; depth grid edge shouldn't reject."""
    grid = _depth_grid_with_shore()
    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", return_value=None):
            is_navigable = make_navigable_predicate("conus", draft_m=2.0)
    # Far north, outside depth grid → True (defer to wind grid bounds)
    assert is_navigable(50.0, -87.5) is True


def test_hazard_polygon_blocks_otherwise_deep_water():
    grid = _depth_grid_with_shore()
    haz = _hazard_index_with_one_polygon()
    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", return_value=haz):
            is_navigable = make_navigable_predicate("conus", draft_m=2.0)

    # Inside the hazard polygon, even though depth is fine
    assert is_navigable(42.10, -87.50) is False
    # Just outside the polygon — same deep water — navigable
    assert is_navigable(42.20, -87.50) is True


def test_safety_factor_changes_min_depth():
    grid = _depth_grid_with_shore()
    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", return_value=None):
            # 2m draft × 2.5 = 5m min — even the 2m shoal (lon=-87.7) is still blocked
            tight = make_navigable_predicate("conus", draft_m=2.0, safety_factor=2.5)
            # 2m draft × 1.0 = 2m min — exactly the shoal boundary
            loose = make_navigable_predicate("conus", draft_m=2.0, safety_factor=1.0)

    assert tight(42.0, -87.7) is False
    assert loose(42.0, -87.7) is True


def test_missing_bathymetry_raises():
    from app.services.bathymetry import BathymetryUnavailable
    with patch.object(bathymetry, "_load_from_gcs", return_value=None):
        with pytest.raises(BathymetryUnavailable):
            make_navigable_predicate("conus", draft_m=2.0)
