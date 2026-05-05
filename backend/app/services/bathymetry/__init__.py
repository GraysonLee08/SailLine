"""Bathymetry service — depth grids loaded from GCS, sampled per-point.

Depth grids are produced by ``backend/workers/bathymetry_ingest.py`` from
NOAA NCEI sources (Great Lakes bathymetry, Coastal Relief Model volumes)
and stored as compressed ``.npz`` files at ``bathymetry/{region}/depth.npz``
in the project's GCS bucket.

NOTE: v1 reuses the existing ``sailline-weather`` bucket via
``settings.gcs_weather_bucket``. That bucket has a 30-day delete-all
lifecycle rule (set up for GRIB cleanup), which means bathymetry files
get auto-deleted monthly. The ingest worker run is cheap (~10s with
cached download) so re-running monthly is fine for v1. Long-term fix:
either split into a dedicated bucket or change the lifecycle rule to
only match ``gfs/`` and ``hrrr/`` prefixes.

Each region's grid is loaded into memory once on first ``for_region(name)``
call and cached for the life of the process. Grids are typically 2–100 MB;
Cloud Run containers run with 512 MB+ memory and the grids load in
hundreds of milliseconds.

Sign convention: depth values are POSITIVE meters below datum (water
surface). Land returns negative or zero. The router asks "is depth at
(lat, lon) >= my minimum?" — that comparison naturally rejects land.

Datum: Lake Michigan grids use Low Water Datum (IGLD85). CRM volumes use
MLLW for ocean coverage. Both are conservative — the actual water surface
is typically above datum, so quoted depths are lower bounds. Lake-level /
tide correction is a v1.x feature.
"""
from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
from google.cloud import storage
from google.cloud.exceptions import NotFound

from app.config import settings

log = logging.getLogger(__name__)


# ─── Data class ─────────────────────────────────────────────────────────


@dataclass
class DepthGrid:
    """Regular lat/lon depth grid with bilinear sampling.

    lats: 1D ascending (degrees)
    lons: 1D ascending (degrees)
    depth_m: 2D shape (len(lats), len(lons)). Positive = below datum,
        negative = land. NaN allowed for cells outside source coverage
        (e.g. CRM tile boundaries) — callers must handle.
    """
    lats: np.ndarray
    lons: np.ndarray
    depth_m: np.ndarray
    region: str
    source: str          # "ncei_great_lakes", "ncei_crm_vol6", etc.
    datum: str           # "LWD", "MLLW"

    def contains(self, lat: float, lon: float) -> bool:
        return (
            self.lats[0] <= lat <= self.lats[-1]
            and self.lons[0] <= lon <= self.lons[-1]
        )

    def sample(self, lat: float, lon: float) -> Optional[float]:
        """Bilinear depth at (lat, lon). None if outside the grid.

        Returns NaN if the bracketing cells are NaN (no coverage). Callers
        treating "no data" as land/avoid is the safe default.
        """
        if not self.contains(lat, lon):
            return None

        i = int(np.searchsorted(self.lats, lat, side="right") - 1)
        j = int(np.searchsorted(self.lons, lon, side="right") - 1)
        i = min(max(i, 0), len(self.lats) - 2)
        j = min(max(j, 0), len(self.lons) - 2)

        lat0, lat1 = self.lats[i], self.lats[i + 1]
        lon0, lon1 = self.lons[j], self.lons[j + 1]
        fy = (lat - lat0) / (lat1 - lat0) if lat1 > lat0 else 0.0
        fx = (lon - lon0) / (lon1 - lon0) if lon1 > lon0 else 0.0

        d00 = self.depth_m[i, j]
        d01 = self.depth_m[i, j + 1]
        d10 = self.depth_m[i + 1, j]
        d11 = self.depth_m[i + 1, j + 1]

        # If any corner is NaN, return NaN — caller decides to fail safe
        if np.isnan(d00) or np.isnan(d01) or np.isnan(d10) or np.isnan(d11):
            return float("nan")

        return float(
            (1 - fx) * (1 - fy) * d00
            + fx * (1 - fy) * d01
            + (1 - fx) * fy * d10
            + fx * fy * d11
        )


# ─── Loader ─────────────────────────────────────────────────────────────


_CACHE: dict[str, Optional[DepthGrid]] = {}
_CACHE_LOCK = threading.Lock()


class BathymetryUnavailable(Exception):
    """Raised when no depth grid is available for the requested region."""


def _gcs_path(region: str) -> str:
    return f"bathymetry/{region}/depth.npz"


def _load_from_gcs(region: str) -> Optional[DepthGrid]:
    """Pull the packed .npz from GCS and unpack into a DepthGrid.

    Returns None if the object doesn't exist (region not yet ingested).
    Raises on any other GCS error so the caller can surface infrastructure
    problems rather than silently routing without depth data.
    """
    if not settings.gcs_weather_bucket:
        log.error("GCS_WEATHER_BUCKET not configured; bathymetry disabled")
        return None

    client = storage.Client()
    bucket = client.bucket(settings.gcs_weather_bucket)
    blob = bucket.blob(_gcs_path(region))

    try:
        raw = blob.download_as_bytes()
    except NotFound:
        log.warning("no bathymetry on GCS for region=%s", region)
        return None

    with np.load(io.BytesIO(raw)) as data:
        return DepthGrid(
            lats=np.asarray(data["lats"], dtype=np.float64),
            lons=np.asarray(data["lons"], dtype=np.float64),
            depth_m=np.asarray(data["depth_m"], dtype=np.float32),
            region=region,
            source=str(data["source"]),
            datum=str(data["datum"]),
        )


def for_region(region: str) -> DepthGrid:
    """Get the depth grid for a region, loading from GCS on first use.

    Raises ``BathymetryUnavailable`` if the region has no ingested grid.
    The router catches this and returns a 503 with a clear message —
    failing-open here would silently route through land, which is worse
    than failing the request.
    """
    with _CACHE_LOCK:
        if region in _CACHE:
            cached = _CACHE[region]
            if cached is None:
                raise BathymetryUnavailable(
                    f"bathymetry not ingested for region={region!r}"
                )
            return cached

        grid = _load_from_gcs(region)
        _CACHE[region] = grid
        if grid is None:
            raise BathymetryUnavailable(
                f"bathymetry not ingested for region={region!r}"
            )

        log.info(
            "loaded bathymetry region=%s shape=%sx%s source=%s datum=%s",
            region, len(grid.lats), len(grid.lons), grid.source, grid.datum,
        )
        return grid


def invalidate_cache(region: Optional[str] = None) -> None:
    """Drop cached grids. Useful in tests; harmless in prod."""
    with _CACHE_LOCK:
        if region is None:
            _CACHE.clear()
        else:
            _CACHE.pop(region, None)


__all__ = ["DepthGrid", "BathymetryUnavailable", "for_region", "invalidate_cache"]
