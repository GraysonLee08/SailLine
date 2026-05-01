"""Region registry — single source of truth for what wind grids we ingest.

A "region" is a pre-clipped bbox plus the set of NOAA sources we run for it.
Adding a region is a one-stop edit: append to REGIONS, deploy, then provision
a Cloud Run Job + Scheduler trigger per (source, region) — see
docs/multi-region-rollout.md.

The frontend has its own mirror (frontend/src/lib/regions.js). When you edit
this file, edit that one too. Keep them in sync; the names below are the
public contract for /api/weather?region=...

HRRR is CONUS-only — Hawaii is GFS-only because HRRR doesn't cover it.
The rest of these regions sit comfortably inside HRRR's domain.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    name: str                                    # url-safe id, e.g. "great_lakes"
    label: str                                   # human-facing, e.g. "Great Lakes"
    bbox: tuple[float, float, float, float]      # min_lat, max_lat, min_lon, max_lon
    sources: tuple[str, ...]                     # which NOAA sources we ingest

    def contains(self, lat: float, lon: float) -> bool:
        min_lat, max_lat, min_lon, max_lon = self.bbox
        return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


# All regions are ingested. Order here is the order shown to users if a UI
# ever needs to list them (today nothing does — region selection is implicit
# from location).
REGIONS: dict[str, Region] = {
    "great_lakes": Region(
        name="great_lakes",
        label="Great Lakes",
        bbox=(40.0, 50.0, -94.0, -75.0),
        sources=("hrrr", "gfs"),
    ),
    "chesapeake": Region(
        name="chesapeake",
        label="Chesapeake Bay",
        bbox=(36.5, 39.5, -77.5, -75.5),
        sources=("hrrr", "gfs"),
    ),
    "long_island_sound": Region(
        name="long_island_sound",
        label="Long Island Sound",
        bbox=(40.5, 41.5, -74.0, -71.5),
        sources=("hrrr", "gfs"),
    ),
    "new_england": Region(
        name="new_england",
        label="New England",
        bbox=(40.5, 43.5, -72.0, -69.0),
        sources=("hrrr", "gfs"),
    ),
    "florida": Region(
        name="florida",
        label="South Florida",
        bbox=(24.0, 26.5, -82.0, -79.5),
        sources=("hrrr", "gfs"),
    ),
    "gulf_coast": Region(
        name="gulf_coast",
        label="Gulf Coast",
        bbox=(27.0, 30.5, -94.0, -82.0),
        sources=("hrrr", "gfs"),
    ),
    "socal": Region(
        name="socal",
        label="Southern California",
        bbox=(32.5, 34.5, -120.5, -117.0),
        sources=("hrrr", "gfs"),
    ),
    "sf_bay": Region(
        name="sf_bay",
        label="San Francisco Bay",
        bbox=(37.0, 38.5, -123.5, -121.5),
        sources=("hrrr", "gfs"),
    ),
    "pnw": Region(
        name="pnw",
        label="Pacific Northwest",
        bbox=(47.0, 49.0, -124.0, -122.0),
        sources=("hrrr", "gfs"),
    ),
    "hawaii": Region(
        name="hawaii",
        label="Hawaii",
        bbox=(18.5, 22.5, -161.0, -154.5),
        sources=("gfs",),  # outside HRRR's CONUS domain
    ),
}


def get(name: str) -> Region:
    """Lookup; raises KeyError if unknown."""
    return REGIONS[name]


def all_pairs() -> list[tuple[str, str]]:
    """All (source, region) pairs that should have a worker. Used by tests
    and the rollout docs to sanity-check infra coverage."""
    return [(src, r.name) for r in REGIONS.values() for src in r.sources]
