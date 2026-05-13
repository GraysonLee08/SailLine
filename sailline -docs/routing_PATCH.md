# backend/app/routers/routing.py — patch summary
#
# Two surgical changes. The rest of the file is unchanged.
#
# ─── Change 1: imports ──────────────────────────────────────────────────
#
# Add `venue_for_point` to the existing regions import:
#
#     from app.regions import REGIONS, base_region_for_point, venue_for_point

# ─── Change 2: _resolve_region returns BOTH base and (optional) venue ──
#
# Replace the existing _resolve_region with this:

from typing import Optional


def _resolve_region(marks: list[dict]) -> tuple[str, Optional[str]]:
    """Return (base_region, venue_or_None) for the centroid of the marks.

    Base region drives wind + bathymetry lookup (always set, defaults to
    'conus'). Venue is set only when the centroid falls inside one of
    the high-res venue bboxes — that's the trigger for loading
    harbour-scale ENC hazards.
    """
    if not marks:
        return "conus", None
    lat_c = sum(m["lat"] for m in marks) / len(marks)
    lon_c = sum(m["lon"] for m in marks) / len(marks)
    base = base_region_for_point(lat_c, lon_c)
    venue = venue_for_point(lat_c, lon_c)
    return (
        base.name if base is not None else "conus",
        venue.name if venue is not None else None,
    )


# ─── Change 3: compute_route uses the new return shape ─────────────────
#
# In the body of compute_route, replace:
#
#     region = _resolve_region(marks)
#
# with:
#
#     region, venue = _resolve_region(marks)
#
# And update the debug log to include the venue:
#
#     log.warning(
#         "ROUTING DEBUG region=%s venue=%s has_gfs=%s race_start=%s now+18h=%s",
#         region, venue,
#         "gfs" in REGIONS[region].sources,
#         race_start.isoformat(),
#         (datetime.now(timezone.utc) + timedelta(hours=18)).isoformat(),
#     )
#
# Then thread `venue` into the navigability call. Find the existing
# make_navigable_predicate call (probably looks like):
#
#     is_navigable = make_navigable_predicate(
#         region=region,
#         draft_m=spec.draft_m,
#         safety_factor=payload.safety_factor,
#     )
#
# and change to:
#
#     is_navigable = make_navigable_predicate(
#         region=region,
#         draft_m=spec.draft_m,
#         safety_factor=payload.safety_factor,
#         venue=venue,
#     )
