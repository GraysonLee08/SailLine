"""Additions to test_navigability.py — venue + base merging.

These tests cover the dual-index path: when a race is inside a known
venue, the navigability predicate loads both the base region's hazards
(general scale) and the venue's hazards (harbour scale). A polygon
in either index blocks the route.

The ``_depth_grid_with_shore`` helper is intentionally duplicated from
``test_navigability.py`` to keep each test file self-contained — pytest
collection across files is fragile when one test module imports another.
The two copies are tiny and identical; if they ever diverge, that's a
test failure waiting to happen, not a design flaw.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from app.services import bathymetry, charts
from app.services.bathymetry import DepthGrid
from app.services.charts import HazardIndex
from app.services.routing.navigability import make_navigable_predicate


@pytest.fixture(autouse=True)
def clear_caches():
    bathymetry.invalidate_cache()
    charts.invalidate_cache()
    yield
    bathymetry.invalidate_cache()
    charts.invalidate_cache()


def _depth_grid_with_shore() -> DepthGrid:
    """Lat/lon grid where lon < -87.7 is land (negative depth) and lon >= -87.5 is water (50m).

    Duplicated from test_navigability.py for file isolation.
    """
    lats = np.array([41.5, 42.0, 42.5])
    lons = np.array([-88.0, -87.7, -87.5, -87.0])
    row = np.array([-10.0, 2.0, 50.0, 50.0], dtype=np.float32)
    depth = np.tile(row, (3, 1))
    return DepthGrid(
        lats=lats, lons=lons, depth_m=depth,
        region="conus", source="synthetic", datum="LWD",
    )


def _hazard_index(name: str, polygon: Polygon, layer: str) -> HazardIndex:
    return HazardIndex(
        polygons=[polygon], tree=STRtree([polygon]),
        region=name, source_layers=(layer,), feature_count=1,
    )


def test_venue_hazard_blocks_when_base_is_clear():
    """A breakwall in the venue index blocks routing even if the base
    index has nothing there."""
    grid = _depth_grid_with_shore()

    # Base (conus) has no hazards. Venue (chicago) has one polygon
    # representing a breakwall in deep water.
    breakwall = Polygon([
        (-87.55, 42.05),
        (-87.45, 42.05),
        (-87.45, 42.15),
        (-87.55, 42.15),
    ])
    chicago_haz = _hazard_index("chicago", breakwall, "SLCONS")

    def fake_loader(region):
        if region == "chicago":
            return chicago_haz
        return None  # base 'conus' has nothing

    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", side_effect=fake_loader):
            is_navigable = make_navigable_predicate(
                "conus", draft_m=2.0, venue="chicago",
            )

    # Inside the breakwall polygon — blocked
    assert is_navigable(42.10, -87.50) is False
    # Just outside the polygon — same deep water — navigable
    assert is_navigable(42.20, -87.50) is True


def test_base_hazard_still_applies_when_venue_set():
    """Both indices are checked. A polygon only in the base index still
    blocks even when a venue is also loaded."""
    grid = _depth_grid_with_shore()

    big_offshore = Polygon([
        (-87.55, 42.05),
        (-87.45, 42.05),
        (-87.45, 42.15),
        (-87.55, 42.15),
    ])
    base_haz = _hazard_index("conus", big_offshore, "OBSTRN")
    # Venue index exists but is empty
    empty_venue = HazardIndex(
        polygons=[], tree=STRtree([]),
        region="chicago", source_layers=(), feature_count=0,
    )

    def fake_loader(region):
        return base_haz if region == "conus" else empty_venue

    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", side_effect=fake_loader):
            is_navigable = make_navigable_predicate(
                "conus", draft_m=2.0, venue="chicago",
            )

    assert is_navigable(42.10, -87.50) is False


def test_venue_none_falls_back_to_base_only():
    """Existing single-region behaviour is preserved when venue=None."""
    grid = _depth_grid_with_shore()

    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", return_value=None):
            is_navigable = make_navigable_predicate("conus", draft_m=2.0)

    assert is_navigable(42.0, -87.5) is True
    assert is_navigable(42.0, -88.0) is False
