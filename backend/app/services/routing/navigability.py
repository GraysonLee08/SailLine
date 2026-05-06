"""Navigability predicates for the isochrone engine.

Combines two safety layers into ``(lat, lon) -> bool`` and
``(lat1, lon1, lat2, lon2) -> bool`` predicates that the engine
consumes. Two predicates because the cost/precision trade-offs differ:

  1. Bathymetry depth >= draft × safety_factor
  2. Point not inside / segment not crossing any ENC hazard polygon

The point predicate (``is_navigable``) is what tests + scripts have
always used. It samples depth at one (lat, lon) and runs a point-in-
polygon test against hazards.

The segment predicate (``is_navigable.segment``) is what the engine
actually wants for every isochrone move. It samples depth along the
line, then runs an exact ``LineString.intersects(Polygon)`` test for
hazards. The exact line test catches thin polygons (breakwalls,
narrow islands) that a point-sampler would miss between samples — a
20 m wide breakwall is ~20 % detectable by 100 m sampling but 100 %
detectable by line intersection.

Hazards are loaded per-region. When a race is inside a known venue
(chicago, sf_bay, ...), the predicate loads BOTH the base region's
hazards (general-scale: open-water obstructions, military areas) AND
the venue's hazards (harbour-scale: breakwalls, jetties, fishing
facilities). A point/segment is hazardous if it touches any polygon
from either index. The two scales complement each other — base
catches things outside the venue bbox, venue catches things too small
to appear at base scale.

Bathymetry "no data is hazardous" rule: NCEI grids have NaN cells at
coverage edges (e.g. tile boundaries between CRM volumes). Treating
NaN as land means the engine routes around data gaps rather than
through them — the right safety default. If a user complains that the
route avoids open water near a CRM tile boundary, the answer is to
ingest the adjacent volume, not to fail-open on NaN.

Log levels: the per-call status line is at WARNING so it surfaces in
Cloud Run's default text-payload feed (INFO from app loggers is
filtered). Drop back to INFO once we have proper structured logging
configured.
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Optional, Protocol

from app.services import bathymetry, charts


log = logging.getLogger(__name__)


# Default safety factor on draft. 1.5× is a common cruising rule of thumb
# (3 ft buffer for a 6 ft draft). Pro racers run tighter (1.2–1.3×) in
# calm water; buoy racers in shallow venues run 1.5–2×. Configurable per
# race in v1.x; hardcoded for v1.
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


class NavigablePredicate(Protocol):
    """Point predicate with an attached `segment` callable.

    The engine duck-types: it tries `is_navigable.segment(...)` first
    and falls back to per-point sampling if absent. Tests that pre-date
    the segment API keep working — they just don't get the precision
    upgrade.
    """
    def __call__(self, lat: float, lon: float) -> bool: ...
    segment: Callable[[float, float, float, float], bool]


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
        Callable ``is_navigable(lat, lon) -> bool`` with an attached
        ``is_navigable.segment(lat1, lon1, lat2, lon2) -> bool``.

    Raises:
        bathymetry.BathymetryUnavailable: if no depth grid is ingested
            for the base region. Caller should surface a 503 — silently
            routing without depth checks is unsafe.
    """
    min_depth_m = draft_m * safety_factor

    # Bathymetry is required. Will raise if not ingested for this region.
    depth_grid = bathymetry.for_region(region)

    # Charts are optional. Build a list of zero-or-more loaded indices.
    hazard_indices: list[charts.HazardIndex] = []
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

    def _depth_ok(lat: float, lon: float) -> Optional[bool]:
        """True if depth OK, False if shallow/land, None if outside grid."""
        depth = depth_grid.sample(lat, lon)
        if depth is None:
            # Outside the grid bounds — fail open at the edges. The
            # engine's wind grid already constrains the search; if a
            # candidate position is outside the depth grid but inside
            # the wind grid, that's a configuration mismatch worth
            # surfacing as a user-visible issue, not a routing failure.
            return None
        if math.isnan(depth) or depth < min_depth_m:
            return False
        return True

    def is_navigable(lat: float, lon: float) -> bool:
        depth = _depth_ok(lat, lon)
        if depth is False:
            return False
        # Hazard check — point is blocked if ANY loaded index covers it.
        for idx in hazard_indices:
            if idx.intersects(lat, lon):
                return False
        return True

    def is_navigable_segment(
        lat1: float, lon1: float, lat2: float, lon2: float,
    ) -> bool:
        # Depth check: sample along the segment. Bathymetry grids are
        # smooth, so a moderately coarse sample step is fine.
        distance_m = _haversine_m(lat1, lon1, lat2, lon2)
        if distance_m > 0:
            n_depth_samples = max(2, int(math.ceil(distance_m / DEPTH_SEGMENT_STEP_M)))
            heading = _bearing_deg(lat1, lon1, lat2, lon2)
            for i in range(n_depth_samples + 1):
                d = distance_m * i / n_depth_samples
                chk_lat, chk_lon = _project(lat1, lon1, heading, d)
                depth = _depth_ok(chk_lat, chk_lon)
                if depth is False:
                    return False
        else:
            depth = _depth_ok(lat1, lon1)
            if depth is False:
                return False

        # Hazard check: exact line-vs-polygon intersection. This catches
        # any polygon the segment touches, regardless of polygon
        # thickness — the whole point of using LineString over point
        # sampling.
        for idx in hazard_indices:
            if idx.crosses_line(lat1, lon1, lat2, lon2):
                return False
        return True

    is_navigable.segment = is_navigable_segment  # type: ignore[attr-defined]
    return is_navigable  # type: ignore[return-value]


__all__ = ["make_navigable_predicate", "DEFAULT_SAFETY_FACTOR", "NavigablePredicate"]
