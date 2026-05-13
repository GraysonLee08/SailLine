# backend/tests/test_currents_regions.py
"""Tests for the currents source registry.

Covers:
  - Registry contents (every documented source present, correct grid type)
  - bbox containment + overlap
  - URL construction matches NOAA NOMADS file naming convention
  - fhour_range parameterisation by run type
  - sources_covering_marks selects the right source for known race courses
"""
from __future__ import annotations

import pytest

from app.currents_regions import (
    CURRENT_SOURCES,
    all_source_names,
    by_grid_type,
    get,
    sources_covering_marks,
    sources_covering_point,
)


# ─── Registry contents ──────────────────────────────────────────────────


def test_all_documented_sources_present():
    """Smoke check — every source we plan to deploy must be registered."""
    expected = {
        # Great Lakes (FVCOM)
        "lmhofs", "lsofs", "leofs", "loofs",
        # Coastal FVCOM
        "sfbofs",
        # Coastal ROMS
        "cbofs", "dbofs", "tbofs", "gomofs", "ngofs2",
        # Coastal POM
        "nyofs",
    }
    assert expected.issubset(set(all_source_names()))


def test_grid_type_split():
    """Each grid family is non-empty so the loader's dispatch is exercised."""
    assert by_grid_type("fvcom"), "FVCOM sources must exist"
    assert by_grid_type("roms"), "ROMS sources must exist"
    assert by_grid_type("pom"), "POM sources must exist"


def test_get_unknown_source_raises():
    with pytest.raises(KeyError):
        get("nope_ofs")


# ─── Bbox containment / overlap ─────────────────────────────────────────


def test_contains_inside_and_outside():
    lmhofs = get("lmhofs")
    # Chicago harbour — well inside LMHOFS
    assert lmhofs.contains(41.9, -87.6)
    # Lake Superior — outside LMHOFS but inside LSOFS
    assert not lmhofs.contains(47.5, -88.0)
    assert get("lsofs").contains(47.5, -88.0)


def test_overlaps_bbox_partial_overlap():
    """A bbox that crosses the LMHOFS/LEOFS boundary overlaps both."""
    # Detroit / Lake St. Clair area straddles LMHOFS east edge and is
    # west of LEOFS proper — exact behaviour depends on registry tuning,
    # but at minimum a wider bbox that overlaps both should match both.
    wide_bbox = (41.5, 43.0, -83.5, -82.5)
    overlapping = [s.name for s in CURRENT_SOURCES.values()
                   if s.overlaps_bbox(wide_bbox)]
    assert "lmhofs" in overlapping
    assert "leofs" in overlapping


def test_overlaps_bbox_disjoint():
    """An ocean bbox south of every OFS source should match nothing."""
    # Mid-Pacific, no OFS coverage at all
    pacific = (20.0, 22.0, -160.0, -158.0)
    overlapping = [s.name for s in CURRENT_SOURCES.values()
                   if s.overlaps_bbox(pacific)]
    assert overlapping == []


# ─── URL builder ────────────────────────────────────────────────────────


def test_url_for_forecast():
    url = get("lmhofs").url_for("f", "20260513", 12, 6)
    assert url == (
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/nos/prod/"
        "lmhofs.20260513/nos.lmhofs.fields.f006.20260513.t12z.nc"
    )


def test_url_for_nowcast():
    url = get("cbofs").url_for("n", "20260513", 18, 1)
    assert url == (
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/nos/prod/"
        "cbofs.20260513/nos.cbofs.fields.n001.20260513.t18z.nc"
    )


def test_url_for_bad_run_type():
    with pytest.raises(ValueError):
        get("lmhofs").url_for("x", "20260513", 12, 6)


# ─── fhour_range ────────────────────────────────────────────────────────


def test_fhour_range_forecast_includes_zero():
    """Forecast range starts at f000 for consistency with weather worker."""
    src = get("lmhofs")
    fhours = src.fhour_range("f")
    assert fhours[0] == 0
    assert fhours[-1] == src.forecast_horizon_hours


def test_fhour_range_nowcast_starts_at_one():
    """Nowcast files start at n001 per NOAA convention; no n000."""
    src = get("lmhofs")
    fhours = src.fhour_range("n")
    assert fhours[0] == 1
    assert fhours[-1] == src.nowcast_horizon_hours


def test_fhour_range_default_is_forecast():
    src = get("lmhofs")
    assert src.fhour_range() == src.fhour_range("f")


def test_fhour_range_bad_run_type():
    with pytest.raises(ValueError):
        get("lmhofs").fhour_range("x")


# ─── Race-time lookups ──────────────────────────────────────────────────


def test_sources_covering_point_chicago():
    """A Chicago lakefront mark must resolve to LMHOFS."""
    matches = [s.name for s in sources_covering_point(41.9, -87.6)]
    assert matches == ["lmhofs"]


def test_sources_covering_point_no_coverage():
    """A mid-ocean Hawaii point has no OFS coverage."""
    assert sources_covering_point(21.0, -157.8) == []


def test_sources_covering_marks_mac_course():
    """Chicago → Mackinac course should resolve to LMHOFS only."""
    marks = [
        {"lat": 41.88, "lon": -87.60},  # Chicago start
        {"lat": 45.85, "lon": -84.62},  # Mackinac Island
    ]
    matches = [s.name for s in sources_covering_marks(marks)]
    assert matches == ["lmhofs"]


def test_sources_covering_marks_empty_returns_empty():
    assert sources_covering_marks([]) == []


def test_sources_covering_marks_no_coverage_returns_empty():
    """A race in a region without OFS support returns the empty list,
    which the router/loader interprets as 'no currents available'."""
    marks = [
        {"lat": 21.0, "lon": -157.8},  # Hawaii
        {"lat": 21.5, "lon": -157.5},
    ]
    assert sources_covering_marks(marks) == []
