"""Additions to test_navigability.py — venue + base merging.

Append these tests to the existing test file. Existing tests still pass
because the new `venue` parameter defaults to None.
"""
from unittest.mock import patch

from shapely.geometry import Polygon
from shapely.strtree import STRtree

from app.services import bathymetry, charts
from app.services.charts import HazardIndex
from app.services.routing.navigability import make_navigable_predicate


def _hazard_index(name: str, polygon: Polygon, layer: str) -> HazardIndex:
    return HazardIndex(
        polygons=[polygon], tree=STRtree([polygon]),
        region=name, source_layers=(layer,), feature_count=1,
    )


def test_venue_hazard_blocks_when_base_is_clear(_depth_grid_with_shore):
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


def test_base_hazard_still_applies_when_venue_set(_depth_grid_with_shore):
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


def test_venue_none_falls_back_to_base_only(_depth_grid_with_shore):
    """Existing single-region behaviour is preserved when venue=None."""
    grid = _depth_grid_with_shore()

    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", return_value=None):
            is_navigable = make_navigable_predicate("conus", draft_m=2.0)

    assert is_navigable(42.0, -87.5) is True
    assert is_navigable(42.0, -88.0) is False
