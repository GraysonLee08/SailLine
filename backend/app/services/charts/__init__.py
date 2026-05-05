"""Chart hazards service — ENC polygons loaded from GCS, queried per-point.

Hazard polygons are produced by ``backend/workers/enc_ingest.py`` from
NOAA ENC Direct REST queries and stored as a single GeoJSON
FeatureCollection at ``charts/{region}/hazards.geojson`` in the project's
GCS bucket. Each region's hazards are loaded into a shapely ``STRtree`` on
first ``for_region(name)`` call and cached for the life of the process.

Storage: shares the ``sailline-weather`` bucket via
``settings.gcs_weather_bucket`` for v1. See the equivalent note in
``app.services.bathymetry`` about the bucket's lifecycle rule.

Layers we extract from ENC:

  - LNDARE   — Land area (most important; subsumes the hand-eyeballed shoreline)
  - UWTROC   — Underwater rocks awash or above water
  - OBSTRN   — Generic obstructions (pilings, mooring fields, etc.)
  - WRECKS   — Submerged wrecks (with depth attribute, but we treat all as hazards)
  - PIPSOL   — Submerged pipelines
  - RESARE   — Restricted areas (security zones, naval ranges, no-go zones)

A point is "hazardous" iff it falls inside any of these polygons. The
predicate is monotonic across layers — adding more layers only increases
the no-go area, never reduces it.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Optional

from google.cloud import storage
from google.cloud.exceptions import NotFound
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from app.config import settings

log = logging.getLogger(__name__)


# ─── Data class ─────────────────────────────────────────────────────────


@dataclass
class HazardIndex:
    """Spatial index of hazard polygons for a region."""
    polygons: list[BaseGeometry]
    tree: STRtree
    region: str
    source_layers: tuple[str, ...]
    feature_count: int

    def intersects(self, lat: float, lon: float) -> bool:
        """True iff the (lat, lon) point falls inside any hazard polygon.

        Uses the STRtree to narrow candidates by bounding-box, then runs
        an exact ``covers`` test on each candidate. Sub-millisecond per
        query for thousands of polygons.
        """
        if not self.polygons:
            return False
        point = Point(lon, lat)
        for idx in self.tree.query(point):
            if self.polygons[int(idx)].covers(point):
                return True
        return False


# ─── Loader ─────────────────────────────────────────────────────────────


_CACHE: dict[str, Optional[HazardIndex]] = {}
_CACHE_LOCK = threading.Lock()


class HazardsUnavailable(Exception):
    """Raised when no hazard data is available for the requested region.

    Distinct from BathymetryUnavailable so the router can decide whether
    missing charts should fail or proceed with depth-only routing. v1
    chooses to proceed with depth-only — bathymetry alone catches
    shoreline because land = depth 0.
    """


def _gcs_path(region: str) -> str:
    return f"charts/{region}/hazards.geojson"


def _load_from_gcs(region: str) -> Optional[HazardIndex]:
    if not settings.gcs_weather_bucket:
        log.error("GCS_WEATHER_BUCKET not configured; charts disabled")
        return None

    client = storage.Client()
    bucket = client.bucket(settings.gcs_weather_bucket)
    blob = bucket.blob(_gcs_path(region))

    try:
        raw = blob.download_as_bytes()
    except NotFound:
        log.warning("no hazard charts on GCS for region=%s", region)
        return None

    fc = json.loads(raw.decode("utf-8"))
    polygons: list[BaseGeometry] = []
    layers_seen: set[str] = set()

    for feat in fc.get("features", []):
        try:
            geom = shape(feat["geometry"])
        except Exception:  # noqa: BLE001
            continue
        if geom.is_empty:
            continue
        gt = geom.geom_type
        if gt not in ("Polygon", "MultiPolygon"):
            continue
        polygons.append(geom)
        layer = feat.get("properties", {}).get("layer")
        if layer:
            layers_seen.add(layer)

    if not polygons:
        log.info("region=%s loaded 0 hazard polygons (file present but empty)", region)
        return HazardIndex(
            polygons=[], tree=STRtree([]),
            region=region, source_layers=(), feature_count=0,
        )

    tree = STRtree(polygons)
    return HazardIndex(
        polygons=polygons,
        tree=tree,
        region=region,
        source_layers=tuple(sorted(layers_seen)),
        feature_count=len(polygons),
    )


def for_region(region: str) -> Optional[HazardIndex]:
    """Get the hazard index for a region, loading from GCS on first use.

    Returns None (not raises) if no hazards are ingested. Bathymetry is
    the must-have safety layer; ENC is additive. v1 callers proceed
    without ENC if it's missing.
    """
    with _CACHE_LOCK:
        if region in _CACHE:
            return _CACHE[region]

        index = _load_from_gcs(region)
        _CACHE[region] = index
        if index is not None:
            log.info(
                "loaded charts region=%s features=%s layers=%s",
                region, index.feature_count, index.source_layers,
            )
        return index


def invalidate_cache(region: Optional[str] = None) -> None:
    with _CACHE_LOCK:
        if region is None:
            _CACHE.clear()
        else:
            _CACHE.pop(region, None)


__all__ = ["HazardIndex", "HazardsUnavailable", "for_region", "invalidate_cache"]
