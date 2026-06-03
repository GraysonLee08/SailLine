# backend/tests/test_navigability_protocol.py
"""Pin the Phase-3 navigability seam.

Before Phase 3, ``make_navigable_predicate`` returned a callable with
a dynamically-attached ``.segment``, and the engine duck-typed it via
``getattr(p, "segment", None)``. Tests that handed the engine a bare
``lambda`` silently hit a per-point fallback path — production used
exact line-vs-polygon checks, tests used per-point sampling. Two
precisions, one engine, silent divergence.

These tests pin the new contract:

* :class:`NavigablePredicate` is ``@runtime_checkable`` and requires
  both ``__call__`` and ``segment``.
* :func:`always_navigable` and :func:`from_point_func` build
  Protocol-shaped objects.
* The production :func:`make_navigable_predicate` return value
  satisfies the Protocol.
* The engine accepts ``None`` and supplies its own
  :func:`always_navigable` default — no bare-callable code path
  remains.
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
from app.services.routing.navigability import (
    NavigablePredicate,
    always_navigable,
    from_point_func,
    make_navigable_predicate,
)


@pytest.fixture(autouse=True)
def clear_caches():
    bathymetry.invalidate_cache()
    charts.invalidate_cache()
    yield
    bathymetry.invalidate_cache()
    charts.invalidate_cache()


# ─── Protocol membership ────────────────────────────────────────────────


def test_always_navigable_satisfies_protocol():
    p = always_navigable()
    assert isinstance(p, NavigablePredicate)
    assert p(42.0, -87.5) is True
    assert p.segment(42.0, -87.5, 42.1, -87.4) is True


def test_always_navigable_returns_singleton():
    """Identity check — every call hands back the same instance."""
    assert always_navigable() is always_navigable()


def test_from_point_func_satisfies_protocol():
    p = from_point_func(lambda lat, lon: lon < -87.6)
    assert isinstance(p, NavigablePredicate)
    assert p(42.0, -87.7) is True
    assert p(42.0, -87.5) is False
    # Segment OK iff both endpoints OK.
    assert p.segment(42.0, -87.7, 42.0, -87.65) is True
    assert p.segment(42.0, -87.7, 42.0, -87.5) is False


def test_bare_callable_does_not_satisfy_protocol():
    """The previous regression — a lambda alone has no ``segment`` —
    must now fail the runtime check."""
    assert not isinstance(lambda lat, lon: True, NavigablePredicate)


# ─── Production predicate satisfies the Protocol ────────────────────────


def _depth_grid_deep() -> DepthGrid:
    """All-deep grid covering Lake Michigan-ish lat/lon."""
    lats = np.array([41.5, 42.0, 42.5])
    lons = np.array([-88.0, -87.5, -87.0])
    return DepthGrid(
        lats=lats, lons=lons,
        depth_m=np.full((3, 3), 50.0, dtype=np.float32),
        region="conus", source="synthetic", datum="LWD",
    )


def test_make_navigable_predicate_returns_protocol():
    grid = _depth_grid_deep()
    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", return_value=None):
            p = make_navigable_predicate("conus", draft_m=2.0)

    assert isinstance(p, NavigablePredicate)
    assert p(42.0, -87.5) is True
    assert p.segment(42.0, -87.5, 42.1, -87.4) is True


def test_make_navigable_predicate_segment_catches_hazard_polygon():
    """Sanity: the production segment method still does an exact
    line-vs-polygon intersection. Phase 3 didn't change the algorithm,
    only the type-shape it's exposed as."""
    grid = _depth_grid_deep()
    # Polygon centered near (42.05, -87.7).
    poly = Polygon([
        (-87.75, 42.00),
        (-87.65, 42.00),
        (-87.65, 42.10),
        (-87.75, 42.10),
    ])
    haz = HazardIndex(
        polygons=[poly], tree=STRtree([poly]),
        region="conus", source_layers=("OBSTRN",), feature_count=1,
    )

    with patch.object(bathymetry, "_load_from_gcs", return_value=grid):
        with patch.object(charts, "_load_from_gcs", return_value=haz):
            p = make_navigable_predicate("conus", draft_m=2.0)

    # A line that passes through the polygon must be blocked.
    assert p.segment(42.05, -87.80, 42.05, -87.60) is False
    # A line that goes around it (north of) must be fine.
    assert p.segment(42.20, -87.80, 42.20, -87.60) is True


# ─── Engine no-input default ────────────────────────────────────────────


def test_engine_runs_with_is_navigable_none():
    """The engine's ``is_navigable=None`` substitution path is the only
    code site that doesn't take a user-supplied predicate. It must
    produce a Protocol-shaped object — otherwise the engine's
    ``.segment`` call would AttributeError. Smoke-test by running a
    minimal compute with ``is_navigable=None`` and confirming the run
    completes (whether or not it 'reaches' is irrelevant)."""
    from app.services.polars import load_polar
    from app.services.routing.isochrone import (
        WindField,
        compute_isochrone_route,
    )

    wind = WindField(
        lats=np.array([41.0, 42.0, 43.0]),
        lons=np.array([-88.0, -87.5, -87.0]),
        u=np.zeros((3, 3), dtype=np.float32),
        v=np.full((3, 3), 5.0, dtype=np.float32),
        valid_time="2026-01-01T00:00:00+00:00",
        source="test",
    )
    polar = load_polar("app/services/polars/beneteau_36_7.csv")
    # Should not raise — engine substitutes always_navigable() under None.
    result = compute_isochrone_route(
        start=(42.0, -87.5),
        finish=(42.1, -87.4),
        polar=polar,
        wind=wind,
        is_navigable=None,
        max_iterations=5,
    )
    # We don't care about reached/not — only that the run didn't blow up
    # trying to call ``.segment`` on a bare callable.
    assert result is not None
