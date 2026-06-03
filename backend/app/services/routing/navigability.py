"""Navigability predicates for the isochrone engine.

Combines two safety layers into a Protocol-shaped object exposing
``__call__(lat, lon) -> bool`` and ``segment(lat1, lon1, lat2, lon2) -> bool``:

  1. Bathymetry depth >= draft × safety_factor
  2. Point not inside / segment not crossing any ENC hazard polygon

Two methods because the cost/precision trade-offs differ:

The point method (``__call__``) samples depth at one (lat, lon) and
runs a point-in-polygon test against hazards. Cheap; used for
spot-checks like the rounding-side filter.

The segment method (``.segment``) is what the engine uses for every
isochrone move. It samples depth along the line and runs an exact
``LineString.intersects(Polygon)`` test for hazards. The line test
catches thin polygons (breakwalls, narrow islands) that a point
sampler would miss between samples — a 20 m wide breakwall is ~20 %
detectable by 100 m sampling but 100 % detectable by line
intersection.

Hazards are loaded per-region. When a race is inside a known venue
(chicago, sf_bay, ...), the predicate loads BOTH the base region's
hazards (general-scale: open-water obstructions, military areas) AND
the venue's hazards (harbour-scale: breakwalls, jetties, fishing
facilities). A point/segment is hazardous if it touches any polygon
from either index.

Bathymetry "no data is hazardous" rule: NCEI grids have NaN cells at
coverage edges (e.g. tile boundaries between CRM volumes). Treating
NaN as land means the engine routes around data gaps rather than
through them — the right safety default.

Why a Protocol, not a bare callable
-----------------------------------
Before Phase 3, ``make_navigable_predicate`` returned a function with
a ``.segment`` attribute dynamically attached, and the engine
duck-typed via ``getattr``. Tests that handed the engine a bare
``lambda *a, **k: True`` silently hit a per-point fallback path —
production routes used exact line intersection, tests used per-point
sampling. The behaviour-precision difference quietly diverged.

After Phase 3:

* :class:`NavigablePredicate` is a ``@runtime_checkable`` Protocol
  requiring both methods.
* The engine always calls ``.segment``; there is no fallback.
* Tests build predicates via :func:`always_navigable` or
  :func:`from_point_func` (or any class that satisfies the Protocol).
  No bare lambdas to the engine.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from app.services import bathymetry, charts


log = logging.getLogger(__name__)


# Default safety factor on draft. 1.5× is a common cruising rule of thumb
# (3 ft buffer for a 6 ft draft). Pro racers run tighter (1.2–1.3×) in
# calm water; buoy racers in shallow venues run 1.5–2×.
DEFAULT_SAFETY_FACTOR = 1.5


# Depth sample step along a segment. Bathymetry grids vary smoothly, so
# 100 m is fine — the only risk is a NaN cell tucked between two valid
# cells smaller than the step. CRM tile boundaries are sparse and
# usually run for many km, so the practical detection rate is ~100 %.
DEPTH_SEGMENT_STEP_M = 100.0


# Keep haversine + bearing here; importing from isochrone would create
# a circular dependency (isochrone imports navigability via the
# predicate it consumes).
EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlon_r = math.radians(lon2 - lon1)
    y = math.sin(dlon_r) * math.cos(lat2_r)
    x = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon_r)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _project(lat: float, lon: float, heading_deg: float, distance_m: float) -> tuple[float, float]:
    ang = distance_m / EARTH_RADIUS_M
    h = math.radians(heading_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(h))
    lon2 = lon1 + math.atan2(
        math.sin(h) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


# ---------------------------------------------------------------------------
# Protocol


@runtime_checkable
class NavigablePredicate(Protocol):
    """Predicate consumed by the isochrone engine.

    Implementations must provide both methods. The engine calls
    ``segment`` for every candidate move (the precise check) and may
    call ``__call__`` for spot-tests like the rounding-side filter.

    Marked ``@runtime_checkable`` so ``isinstance(p, NavigablePredicate)``
    works at runtime — useful in tests that want to assert "the engine
    received a properly-shaped predicate" rather than a bare callable.
    """
    def __call__(self, lat: float, lon: float) -> bool: ...
    def segment(self, lat1: float, lon1: float, lat2: float, lon2: float) -> bool: ...


# ---------------------------------------------------------------------------
# Helper predicates (tests + the engine's no-input default)


class _AlwaysNavigable:
    """Predicate that approves every point and every segment.

    Used as the engine's default when no predicate is supplied, and as
    a convenience for tests that don't exercise navigability. Returning
    a real Protocol-shaped object (rather than a bare callable) keeps
    the engine's ``.segment`` call uniform across all code paths.
    """
    __slots__ = ()

    def __call__(self, lat: float, lon: float) -> bool:  # noqa: ARG002
        return True

    def segment(self, lat1: float, lon1: float, lat2: float, lon2: float) -> bool:  # noqa: ARG002
        return True


_ALWAYS_NAVIGABLE_SINGLETON = _AlwaysNavigable()


def always_navigable() -> NavigablePredicate:
    """Return a predicate that approves all points and segments.

    Singleton — every call returns the same instance, so equality
    comparisons and identity checks behave predictably in tests.
    """
    return _ALWAYS_NAVIGABLE_SINGLETON


@dataclass(frozen=True)
class _FromPointFunc:
    """Adapt a per-point function into a full :class:`NavigablePredicate`.

    The segment method approves the line iff both endpoints satisfy
    the point function. This matches what a naive per-point check at
    the segment endpoints would do — appropriate for tests that want
    to express "block this rectangle" without writing a full segment
    check. NOT a substitute for the production
    ``make_navigable_predicate``, which does exact line-vs-polygon.
    """
    point_fn: Callable[[float, float], bool]

    def __call__(self, lat: float, lon: float) -> bool:
        return self.point_fn(lat, lon)

    def segment(self, lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
        return self.point_fn(lat1, lon1) and self.point_fn(lat2, lon2)


def from_point_func(point_fn: Callable[[float, float], bool]) -> NavigablePredicate:
    """Wrap a per-point callable as a :class:`NavigablePredicate`.

    Convenience for tests that already have a point predicate and
    don't need a separate segment implementation. Production code
    should always go through :func:`make_navigable_predicate`.
    """
    return _FromPointFunc(point_fn)


# ---------------------------------------------------------------------------
# Production predicate


@dataclass
class _RealPredicate:
    """The production navigability predicate.

    Owns the loaded bathymetry grid + zero or more hazard indices.
    Kept as a dataclass (not a closure) so it composes cleanly with
    the Protocol — :func:`make_navigable_predicate` returns an
    instance and the engine sees both methods statically.
    """
    region: str
    venue: Optional[str]
    min_depth_m: float
    depth_grid: object
    hazard_indices: list[object]

    def _depth_ok(self, lat: float, lon: float) -> Optional[bool]:
        """True if depth OK, False if shallow/land, None if outside grid."""
        depth = self.depth_grid.sample(lat, lon)
        if depth is None:
            # Outside the grid bounds — fail open at the edges. The engine's
            # wind grid already constrains the search; an out-of-bounds
            # position there is a config mismatch, not a routing failure.
            return None
        if math.isnan(depth) or depth < self.min_depth_m:
            return False
        return True

    def __call__(self, lat: float, lon: float) -> bool:
        depth = self._depth_ok(lat, lon)
        if depth is False:
            return False
        for idx in self.hazard_indices:
            if idx.intersects(lat, lon):
                return False
        return True

    def segment(self, lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
        # Depth check: sample along the segment. Bathymetry grids are
        # smooth, so a moderately coarse sample step is fine.
        distance_m = _haversine_m(lat1, lon1, lat2, lon2)
        if distance_m > 0:
            n_depth_samples = max(2, int(math.ceil(distance_m / DEPTH_SEGMENT_STEP_M)))
            heading = _bearing_deg(lat1, lon1, lat2, lon2)
            for i in range(n_depth_samples + 1):
                d = distance_m * i / n_depth_samples
                chk_lat, chk_lon = _project(lat1, lon1, heading, d)
                depth = self._depth_ok(chk_lat, chk_lon)
                if depth is False:
                    return False
        else:
            depth = self._depth_ok(lat1, lon1)
            if depth is False:
                return False

        # Hazard check: exact line-vs-polygon intersection. Catches any
        # polygon the segment touches regardless of polygon thickness —
        # the whole point of using LineString over point sampling.
        for idx in self.hazard_indices:
            if idx.crosses_line(lat1, lon1, lat2, lon2):
                return False
        return True


def make_navigable_predicate(
    region: str,
    draft_m: float,
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
    venue: Optional[str] = None,
) -> NavigablePredicate:
    """Build the navigability predicate for a race.

    Args:
        region: base region name (conus, hawaii). Drives bathymetry
            lookup and the broad-scale hazard index.
        draft_m: boat draft in meters.
        safety_factor: multiplier on draft for the depth check.
        venue: optional venue name (chicago, sf_bay, ...). When set, the
            venue's harbour-scale hazard index is loaded in addition to
            the base index. Both are checked for every point/segment.

    Returns:
        A :class:`NavigablePredicate`. Both ``__call__(lat, lon)`` and
        ``segment(lat1, lon1, lat2, lon2)`` are wired against the same
        depth grid + hazard indices.

    Raises:
        bathymetry.BathymetryUnavailable: if no depth grid is ingested
            for the base region. Caller should surface a 503 — silently
            routing without depth checks is unsafe.
    """
    min_depth_m = draft_m * safety_factor

    # Bathymetry is required. Will raise if not ingested for this region.
    depth_grid = bathymetry.for_region(region)

    # Charts are optional. Build a list of zero-or-more loaded indices.
    hazard_indices: list = []
    base_haz = charts.for_region(region)
    if base_haz is not None:
        hazard_indices.append(base_haz)
    if venue is not None:
        venue_haz = charts.for_region(venue)
        if venue_haz is not None:
            hazard_indices.append(venue_haz)

    if not hazard_indices:
        log.warning(
            "navigability for region=%s venue=%s: depth-only "
            "(charts not ingested)",
            region, venue,
        )
    else:
        total = sum(idx.feature_count for idx in hazard_indices)
        log.warning(
            "navigability for region=%s venue=%s: depth + %s hazard "
            "polygons across %s indices (line-intersect mode)",
            region, venue, total, len(hazard_indices),
        )

    return _RealPredicate(
        region=region,
        venue=venue,
        min_depth_m=min_depth_m,
        depth_grid=depth_grid,
        hazard_indices=hazard_indices,
    )


__all__ = [
    "DEFAULT_SAFETY_FACTOR",
    "NavigablePredicate",
    "always_navigable",
    "from_point_func",
    "make_navigable_predicate",
]
