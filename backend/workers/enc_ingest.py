"""ENC chart ingest worker.

Pulls hazard polygons from NOAA's ENC Direct REST service for a region's
bbox, merges layers into a single GeoJSON FeatureCollection, and uploads
to GCS at ``charts/{region}/hazards.geojson``.

REST endpoint: NOAA ENC Direct ArcGIS REST service exposes ENC features
as queryable layers. We hit each layer's ``/query`` endpoint with the
region bbox and ``f=geojson``. Server returns features in EPSG:4326.

ENC Direct rate-limits aggressively. Worker uses ~1s sleeps between layer
queries; if NOAA changes its limits, expect 429s and back off. Run this
once per region — output is static for months at a time (ENC updates
weekly but most layers we consume don't churn).

Layers ingested (per IHO S-101 / S-57 codes):

    LNDARE  — Land area (the big one; subsumes shorelines)
    UWTROC  — Underwater rocks awash or above water
    OBSTRN  — Generic obstructions
    WRECKS  — Submerged wrecks
    PIPSOL  — Submerged pipelines (kedging risk)
    RESARE  — Restricted areas (security zones, naval ranges)

Each ENC Direct layer has a numeric ID. The mapping below is from
ENC Direct's published service definition; if NOAA renumbers we'll get
empty responses and a clear log message — easy to detect.

Usage:

    python -m workers.enc_ingest --region conus
    python -m workers.enc_ingest --region conus --dry-run
"""
from __future__ import annotations

import tempfile
import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from google.cloud import storage

# Make `app.regions` importable when running from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.regions import REGIONS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("enc_ingest")


# ─── Config ─────────────────────────────────────────────────────────────


# NOAA ENC Direct REST root. Layer IDs follow this base.
ENC_BASE = (
    "https://encdirect.noaa.gov/arcgis/rest/services/encdirect/"
    "enc_general/MapServer"
)

# (layer_id, layer_name) — ENC Direct publishes layer names that match
# IHO S-57 acronyms. Layer IDs are stable but if they shift, update here.
HAZARD_LAYERS: list[tuple[int, str]] = [
    # NOTE: layer IDs below are reasonable defaults from ENC Direct's
    # published service definition as of 2025–2026. The worker logs a
    # clear warning if a layer returns 0 features so we notice ID drift.
    (4,  "LNDARE"),    # land area
    (38, "UWTROC"),    # underwater rocks
    (33, "OBSTRN"),    # obstructions
    (40, "WRECKS"),    # wrecks
    (24, "PIPSOL"),    # pipelines (submerged)
    (29, "RESARE"),    # restricted areas
]

# Per-request timeout. ENC Direct can take 30–60s on big bboxes.
REQUEST_TIMEOUT_S = 90

# Sleep between layer requests to play nice with ENC Direct.
LAYER_SLEEP_S = 1.0

# Max features to request per page. ENC Direct caps responses around 1000.
PAGE_SIZE = 1000


# ─── REST client ────────────────────────────────────────────────────────


def query_layer_geojson(
    layer_id: int,
    bbox: tuple[float, float, float, float],
) -> dict:
    """Hit one ENC Direct layer's /query endpoint and return GeoJSON.

    Pages through results if the layer has more features than PAGE_SIZE.
    """
    min_lat, max_lat, min_lon, max_lon = bbox
    base_params = {
        "where": "1=1",
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(PAGE_SIZE),
    }
    all_features: list[dict] = []
    offset = 0

    while True:
        params = dict(base_params)
        params["resultOffset"] = str(offset)
        url = f"{ENC_BASE}/{layer_id}/query?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "sailline-enc-ingest/0.1"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            page = json.loads(resp.read().decode("utf-8"))

        if "error" in page:
            raise RuntimeError(f"ENC Direct error for layer {layer_id}: {page['error']}")

        features = page.get("features", [])
        all_features.extend(features)

        # exceededTransferLimit: more pages available
        if not page.get("exceededTransferLimit") or not features:
            break
        offset += len(features)

    return {
        "type": "FeatureCollection",
        "features": all_features,
    }


# ─── Pipeline ───────────────────────────────────────────────────────────


def merge_layers(
    bbox: tuple[float, float, float, float],
    layers: list[tuple[int, str]],
) -> dict:
    """Query each hazard layer, merge into one FeatureCollection.

    Annotates each feature with its source layer name in properties.layer
    so the API can report which layers contributed.
    """
    merged: list[dict] = []
    layer_counts: dict[str, int] = {}

    for layer_id, layer_name in layers:
        log.info("querying layer %s (%s)", layer_id, layer_name)
        try:
            fc = query_layer_geojson(layer_id, bbox)
        except Exception as exc:  # noqa: BLE001
            log.warning("layer %s (%s) failed: %s", layer_id, layer_name, exc)
            layer_counts[layer_name] = 0
            time.sleep(LAYER_SLEEP_S)
            continue

        n = len(fc.get("features", []))
        layer_counts[layer_name] = n
        if n == 0:
            log.warning(
                "layer %s (%s) returned 0 features — possible ID drift",
                layer_id, layer_name,
            )
        else:
            log.info("  → %s features", n)

        for feat in fc.get("features", []):
            feat.setdefault("properties", {})["layer"] = layer_name
            merged.append(feat)

        time.sleep(LAYER_SLEEP_S)

    return {
        "type": "FeatureCollection",
        "features": merged,
        # Stash counts in the FC for debugging; valid GeoJSON ignores
        # extra top-level keys.
        "_layer_counts": layer_counts,
    }


def upload_to_gcs(blob_bytes: bytes, region: str) -> str:
    bucket_name = os.environ.get("GCS_WEATHER_BUCKET")
    if not bucket_name:
        raise RuntimeError("GCS_WEATHER_BUCKET env var not set")
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(f"charts/{region}/hazards.geojson")
    blob.upload_from_string(blob_bytes, content_type="application/geo+json")
    uri = f"gs://{bucket_name}/{blob.name}"
    log.info("uploaded %.1f MB → %s", len(blob_bytes) / 1e6, uri)
    return uri


def ingest(region_name: str, dry_run: bool = False) -> dict:
    if region_name not in REGIONS:
        raise SystemExit(f"unknown region {region_name!r}. Valid: {sorted(REGIONS)}")

    region = REGIONS[region_name]
    fc = merge_layers(region.bbox, HAZARD_LAYERS)
    n = len(fc["features"])
    log.info("merged %s features across %s layers", n, len(HAZARD_LAYERS))

    blob = json.dumps(fc).encode("utf-8")
    log.info("serialized GeoJSON: %.1f MB", len(blob) / 1e6)

    if dry_run:
        out_path = Path(tempfile.gettempdir()) / "sailline_enc" / f"{region.name}_hazards.geojson"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(blob)
        log.info("dry-run: wrote %s", out_path)
        return {
            "region": region.name,
            "feature_count": n,
            "size_bytes": len(blob),
            "local_path": str(out_path),
            "layer_counts": fc.get("_layer_counts", {}),
        }

    uri = upload_to_gcs(blob, region.name)
    return {
        "region": region.name,
        "feature_count": n,
        "size_bytes": len(blob),
        "uri": uri,
        "layer_counts": fc.get("_layer_counts", {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest NOAA ENC hazard polygons to GCS")
    parser.add_argument("--region", required=True, help=f"region from app.regions ({sorted(REGIONS)})")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = ingest(args.region, dry_run=args.dry_run)
    log.info("done: %s", result)


if __name__ == "__main__":
    main()
