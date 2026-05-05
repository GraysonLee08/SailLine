"""Navigability predicates for the isochrone engine.

Combines two safety layers into a single ``(lat, lon) -> bool`` callable
that the engine consumes via its ``is_navigable`` parameter:

  1. Bathymetry depth >= draft × safety_factor
  2. Point not inside any ENC hazard polygon

Both layers are independent. Bathymetry is required (failing-open here
silently routes through land); ENC is optional (depth alone catches the
biggest hazard, which is shore).

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
from typing import Callable

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
) -> Callable[[float, float], bool]:
    """Build the ``is_navigable`` predicate for a race.

    Raises:
        bathymetry.BathymetryUnavailable: if no depth grid is ingested
            for this region. Caller should surface a 503 — silently
            routing without depth checks is unsafe.
    """
    min_depth_m = draft_m * safety_factor

    # Bathymetry is required. Will raise if not ingested for this region.
    depth_grid = bathymetry.for_region(region)

    # Charts are optional. None means depth-only routing (still safe).
    hazard_index = charts.for_region(region)

    if hazard_index is None:
        log.info(
            "navigability for region=%s: depth-only (charts not ingested)",
            region,
        )
    else:
        log.info(
            "navigability for region=%s: depth + %s hazard polygons",
            region, hazard_index.feature_count,
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

        # Hazard check (only if charts are loaded)
        if hazard_index is not None and hazard_index.intersects(lat, lon):
            return False

        return True

    return is_navigable


__all__ = ["make_navigable_predicate", "DEFAULT_SAFETY_FACTOR"]
