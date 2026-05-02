"""Region registry — single source of truth for what wind grids we ingest.

Two kinds of regions:

* ``base`` — always-on coverage at moderate resolution. There are two:
  ``conus`` (HRRR @ 0.10° + GFS @ 0.25°) and ``hawaii`` (GFS @ 0.25° — HRRR
  is CONUS-only). Frontend always loads one of these for the user.

* ``venue`` — high-resolution HRRR overlays at native 0.027° (~3 km), one
  per popular sailing area. Frontend loads a venue's grid only when the
  user has zoomed in past zoom 11 AND their viewport center is inside the
  venue's bbox. Layered on top of the base grid; base shows through where
  the venue doesn't cover.

Adding a region is a one-stop edit: append to ``REGIONS``, deploy, then
provision a Cloud Run Job + Scheduler trigger per (source, region). See
``docs/conus-migration.md`` for the rollout runbook.

The frontend has its own mirror at ``frontend/src/lib/regions.js``. When
you edit this file, edit that one too. Region names are the public
contract for ``/api/weather?region=...`` — they must match.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    """A pre-clipped wind grid produced by an ingest worker.

    ``bbox`` is (min_lat, max_lat, min_lon, max_lon).

    ``source_resolutions`` is a tuple of ``(source_name, target_deg)``
    pairs — one per source we ingest for this region. Stored as a tuple
    of tuples (not a dict) so the dataclass can stay frozen+hashable.

    ``kind`` is ``"base"`` or ``"venue"`` and drives frontend layering:
    base regions are always-on background coverage; venues are
    higher-res overlays only loaded at high zoom over the venue area.
    """
    name: str
    label: str
    kind: str
    bbox: tuple[float, float, float, float]
    source_resolutions: tuple[tuple[str, float], ...]

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(s for s, _ in self.source_resolutions)

    def resolution_for(self, source: str) -> float:
        for s, deg in self.source_resolutions:
            if s == source:
                return deg
        raise KeyError(f"source {source!r} not configured for region {self.name!r}")

    def contains(self, lat: float, lon: float) -> bool:
        min_lat, max_lat, min_lon, max_lon = self.bbox
        return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


# ---------------------------------------------------------------------------
# Base regions: always-on coverage. The frontend picks one of these as the
# user's "home base" via GPS/IP geolocation, persisted in localStorage.

_BASE: tuple[Region, ...] = (
    # Wide CONUS bbox covering racing waters from the Florida Keys to the
    # St. Lawrence and from the Pacific to the Maine coast. Sized to fit
    # inside HRRR's CONUS domain with a small inland margin (HRRR
    # extends roughly 21–53°N, -135 to -60°W on its native LCC grid).
    # GFS ingests the same bbox at native 0.25°.
    Region(
        name="conus",
        label="Continental US",
        kind="base",
        bbox=(24.0, 50.0, -126.0, -66.0),
        source_resolutions=(
            ("hrrr", 0.10),
            ("gfs", 0.25),
        ),
    ),
    # Hawaii is outside HRRR's CONUS domain — GFS only.
    Region(
        name="hawaii",
        label="Hawaii",
        kind="base",
        bbox=(18.5, 22.5, -161.0, -154.5),
        source_resolutions=(
            ("gfs", 0.25),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Venue regions: high-res HRRR overlays at native 3 km. Sized to actual
# racing waters with a small (~5 km) margin so users can pan around the
# course without losing high-res coverage. Loaded only at zoom ≥ 11.

_VENUES: tuple[Region, ...] = (
    # ── Great Lakes ──────────────────────────────────────────────────
    Region(
        name="chicago",
        label="Chicago / Lake Michigan South",
        kind="venue",
        bbox=(41.6, 42.5, -88.0, -87.2),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="milwaukee",
        label="Milwaukee Bay",
        kind="venue",
        bbox=(42.7, 43.4, -88.1, -87.5),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="detroit",
        label="Lake St. Clair / Detroit",
        kind="venue",
        bbox=(41.9, 43.0, -83.4, -82.4),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="cleveland",
        label="Lake Erie Central",
        kind="venue",
        bbox=(41.4, 42.3, -82.5, -81.4),
        source_resolutions=(("hrrr", 0.027),),
    ),
    # ── West Coast ───────────────────────────────────────────────────
    Region(
        name="sf_bay",
        label="San Francisco Bay",
        kind="venue",
        bbox=(37.4, 38.2, -122.6, -121.9),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="long_beach",
        label="Long Beach / LA",
        kind="venue",
        bbox=(33.5, 33.9, -118.4, -117.9),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="san_diego",
        label="San Diego",
        kind="venue",
        bbox=(32.5, 32.9, -117.4, -117.0),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="puget_sound",
        label="Puget Sound",
        kind="venue",
        bbox=(47.0, 48.9, -122.8, -122.3),  # Tacoma → Bellingham
        source_resolutions=(("hrrr", 0.027),),
    ),
    # ── East Coast ───────────────────────────────────────────────────
    Region(
        name="annapolis",
        label="Chesapeake / Annapolis",
        kind="venue",
        bbox=(38.7, 39.3, -76.7, -76.2),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="newport_ri",
        label="Newport / Narragansett",
        kind="venue",
        bbox=(41.2, 41.7, -71.6, -71.0),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="buzzards_bay",
        label="Buzzards Bay",
        kind="venue",
        bbox=(41.4, 41.8, -71.2, -70.6),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="marblehead",
        label="Marblehead / Boston",
        kind="venue",
        bbox=(42.3, 42.7, -71.0, -70.6),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="charleston",
        label="Charleston",
        kind="venue",
        bbox=(32.5, 32.9, -80.0, -79.5),
        source_resolutions=(("hrrr", 0.027),),
    ),
    # ── Gulf / Florida ───────────────────────────────────────────────
    Region(
        name="biscayne_bay",
        label="Miami / Biscayne Bay",
        kind="venue",
        bbox=(25.4, 25.9, -80.3, -80.0),
        source_resolutions=(("hrrr", 0.027),),
    ),
    Region(
        name="corpus_christi",
        label="Corpus Christi",
        kind="venue",
        bbox=(27.5, 27.9, -97.5, -97.0),
        source_resolutions=(("hrrr", 0.027),),
    ),
)


REGIONS: dict[str, Region] = {r.name: r for r in (*_BASE, *_VENUES)}


# ---------------------------------------------------------------------------
# Helpers


def get(name: str) -> Region:
    """Lookup; raises KeyError if unknown."""
    return REGIONS[name]


def base_regions() -> list[Region]:
    return [r for r in REGIONS.values() if r.kind == "base"]


def venue_regions() -> list[Region]:
    return [r for r in REGIONS.values() if r.kind == "venue"]


def base_region_for_point(lat: float, lon: float) -> Region | None:
    """Find which base region (conus or hawaii) contains (lat, lon).

    Returns None if the point is outside all base regions — caller decides
    whether to fall back to a default."""
    for r in base_regions():
        if r.contains(lat, lon):
            return r
    return None


def venue_for_point(lat: float, lon: float) -> Region | None:
    """Find which venue (if any) contains (lat, lon).

    Venues don't currently overlap, so the first match wins. If two
    venues ever do overlap, this returns the one declared first in
    ``_VENUES`` — adjust the order there if specificity matters."""
    for r in venue_regions():
        if r.contains(lat, lon):
            return r
    return None


def all_pairs() -> list[tuple[str, str]]:
    """All (source, region) pairs that should have an ingest worker.
    Used by tests and the rollout runbook to sanity-check infra coverage."""
    return [(src, r.name) for r in REGIONS.values() for src in r.sources]
