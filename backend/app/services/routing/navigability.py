"""Navigability predicates for the isochrone engine.

Combines two safety layers into a single ``(lat, lon) -> bool`` callable
that the engine consumes via its ``is_navigable`` parameter:

  1. Bathymetry depth >= draft × safety_factor
  2. Point not inside any ENC hazard polygon

Both layers are independent. Bathymetry is required (failing-open here
silently routes through land); ENC is optional (depth alone catches the
biggest hazard, which is shore).

Hazards are loaded per-region. When a race is inside a known venue
(chicago, sf_bay, ...), the predicate loads BOTH the base region's
hazards (general-scale: open-water obstructions, military areas) AND
the venue's hazards (harbour-scale: breakwalls, jetties, fishing
facilities). A point is hazardous if it's inside any polygon from
either index. The two scales complement each other — base catches
things outside the venue bbox, venue catches things too small to
appear at base scale.

The "no data is hazardous" rule for bathymetry is deliberate. NCEI grids
have NaN cells at coverage edges (e.g. tile boundaries between CRM
volumes). Treating NaN as land means the engine routes around data gaps
rather than through them — the right safety default. If a user complains
that the route avoids open water near a CRM tile boundary, the answer is
to ingest the adjacent volume, not to fail-open on NaN.
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Optional

from app.services import bathymetry, charts

log = logging.getLogger(__name__)


# Default safety factor on draft. 1.5× is a common cruising rule of thumb
# (3 ft buffer for a 6 ft draft). Pro racers run tighter (1.2–1.3×) in
# calm water; buoy racers in shallow venues run 1.5–2×. Configurable per
# race in v1.x; hardcoded for v1.
DEFAULT_SAFETY_FACTOR = 1.5


def make_navigable_predicate(
    region: str,
    draft_m: float,
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
    venue: Optional[str] = None,
) -> Callable[[float, float], bool]:
    """Build the ``is_navigable`` predicate for a race.

    Args:
        region: base region name (conus, hawaii). Drives bathymetry
            lookup and the broad-scale hazard index.
        draft_m: boat draft in meters.
        safety_factor: multiplier on draft for the depth check.
        venue: optional venue name (chicago, sf_bay, ...). When set, the
            venue's harbour-scale hazard index is loaded in addition to
            the base index. Both are checked for every point.

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
        log.info(
            "navigability for region=%s venue=%s: depth-only "
            "(charts not ingested)",
            region, venue,
        )
    else:
        total = sum(idx.feature_count for idx in hazard_indices)
        log.info(
            "navigability for region=%s venue=%s: depth + %s hazard "
            "polygons across %s indices",
            region, venue, total, len(hazard_indices),
        )

    def is_navigable(lat: float, lon: float) -> bool:
        # Depth check
        depth = depth_grid.sample(lat, lon)
        if depth is None:
            # Outside the grid bounds — fail open at the edges. The
            # engine's wind grid already constrains the search; if a
            # candidate position is outside the depth grid but inside
            # the wind grid, that's a configuration mismatch worth
            # surfacing as a user-visible issue, not a routing failure.
            return True
        if math.isnan(depth) or depth < min_depth_m:
            return False

        # Hazard check — point is blocked if ANY loaded index covers it.
        # Order doesn't matter (boolean OR), but venue tends to be
        # smaller so STRtree narrows faster — listed in append order
        # (base first) which is fine for typical race geometries.
        for idx in hazard_indices:
            if idx.intersects(lat, lon):
                return False

        return True

    return is_navigable


__all__ = ["make_navigable_predicate", "DEFAULT_SAFETY_FACTOR"]
