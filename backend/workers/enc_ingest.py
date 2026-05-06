"""ENC chart ingest worker.

Pulls hazard polygons from NOAA's ENC Direct REST service for a region's
bbox, merges layers into a single GeoJSON FeatureCollection, and uploads
to GCS at ``charts/{region}/hazards.geojson``.

Two services are used, picked automatically based on ``region.kind``:

  * ``base`` regions (conus, hawaii) hit the **enc_general** service
    (scale 1:600k–1:1.5M). Coarse, but covers the full continent in a
    single ingest. Good enough for offshore-passage routing where
    venue-scale obstructions are far below the noise floor anyway.

  * ``venue`` regions (chicago, sf_bay, ...) hit the **enc_harbour**
    service (scale 1:5k–1:50k). This is where breakwalls, jetties,
    piers, and small-craft facility boundaries actually exist as
    polygons.

Per-layer timeouts: harbour-scale LNDARE / SLCONS / OBSTRN over a full
venue bbox can return thousands of polygons and ENC Direct sometimes
exceeds even a generous 180s timeout. ``query_layer_geojson`` handles
this by recursively quartering the bbox on timeout and deduping
features by their ENC ``OBJECTID``. Layers that come back fast on the
first try (CTNARE, MIPARE, RESARE) skip the chunking path entirely.

Each ENC Direct layer has a numeric ID inside its parent service. IDs
are stable per service — but they are NOT the same across services, so
every layer table here is paired with the service it's valid for. To
verify current IDs, hit ``{ENC_BASE}/{service}/MapServer?f=json`` in
a browser.

Usage (run from backend/):

    python -m workers.enc_ingest --region conus
    python -m workers.enc_ingest --region chicago
    python -m workers.enc_ingest --region chicago --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from google.cloud import storage

# Make `app.regions` importable when running from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.regions import REGIONS, Region  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("enc_ingest")


# ─── Service config ──────────────────────────────────────────────────────


# NOAA ENC Direct REST root. Service name (general / harbour) is
# appended per-region.
ENC_BASE = "https://encdirect.noaa.gov/arcgis/rest/services/encdirect"


# Polygon layers in the **enc_general** service (CONUS / open-ocean
# scale). Verified against the live MapServer manifest, May 2026.
#
# We deliberately skip:
#   - point-only layers (UWTROC, PIPSOL at this scale exist only as
#     points, not polygons — the engine uses point-in-polygon checks)
#   - administrative or descriptive polygons (Sea_Area_Named, EEZ,
#     Coverage_area, ...) — see the navigability module's docstring for
#     the rationale on what counts as a hazard
GENERAL_HAZARD_LAYERS: list[tuple[int, str]] = [
    (121, "LNDARE"),    # General.Land_Area
    (83,  "OBSTRN"),    # General.Obstruction_area
    (84,  "WRECKS"),    # General.Wreck_area
    (82,  "CTNARE"),    # General.Caution_Area
    (86,  "MIPARE"),    # General.Military_Practice_Area
    (102, "RESARE"),    # General.Restricted_Area (filtered at load time)
]


# Polygon layers in the **enc_harbour** service (1:5k–1:50k). Includes
# the layers that don't exist at smaller scales — most importantly
# SLCONS (shoreline construction = breakwalls, jetties, piers).
HARBOUR_HAZARD_LAYERS: list[tuple[int, str]] = [
    (138, "SLCONS"),    # Harbor.Shoreline_Construction_area — breakwalls
    (233, "LNDARE"),    # Harbor.Land_Area
    (156, "OBSTRN"),    # Harbor.Obstruction_area
    (158, "WRECKS"),    # Harbor.Wreck_area
    (154, "CTNARE"),    # Harbor.Caution_Area
    (162, "MIPARE"),    # Harbor.Military_Practice_Area
    (175, "DYKCON"),    # Harbor.Dyke_area — seawalls/levees
    (155, "FSHFAC"),    # Harbor.Fishing_Facility_area — fish traps, weirs
    (197, "RESARE"),    # Harbor.Restricted_Area_area (filtered at load time)
]


def _service_for(region: Region) -> tuple[str, list[tuple[int, str]]]:
    """Pick (service-name, layer-table) for a region based on its kind."""
    if region.kind == "venue":
        return "enc_harbour", HARBOUR_HAZARD_LAYERS
    if region.kind == "base":
        return "enc_general", GENERAL_HAZARD_LAYERS
    raise ValueError(f"unknown region kind {region.kind!r} for {region.name}")


# ─── Knobs ───────────────────────────────────────────────────────────────


REQUEST_TIMEOUT_S = 180     # generous — harbour-scale layers are slow
LAYER_SLEEP_S = 1.0         # be nice to ENC Direct between layer calls
PAGE_SIZE = 1000            # ENC Direct caps single-response features
MIN_CHUNK_DEG = 0.15        # ~10 nm — don't subdivide finer than this


# ─── REST client ─────────────────────────────────────────────────────────


def _query_once(
    service: str,
    layer_id: int,
    bbox: tuple[float, float, float, float],
) -> list[dict]:
    """One bbox query (with internal pagination). Raises on timeout/error.

    Pagination follows ``exceededTransferLimit``. Returns features list,
    not a FeatureCollection.
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
        url = (
            f"{ENC_BASE}/{service}/MapServer/{layer_id}/query?"
            f"{urllib.parse.urlencode(params)}"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "sailline-enc-ingest/0.2"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "")

        try:
            page = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            snippet = raw[:300].decode("utf-8", errors="replace")
            raise RuntimeError(
                f"non-JSON response from {service} layer {layer_id} "
                f"(content-type={content_type!r}): {exc}. "
                f"First 300 chars: {snippet!r}"
            ) from exc

        if "error" in page:
            raise RuntimeError(
                f"ENC Direct error ({service} layer {layer_id}): "
                f"{page['error']}"
            )

        features = page.get("features", [])
        all_features.extend(features)

        if not page.get("exceededTransferLimit") or not features:
            break
        offset += len(features)

    return all_features


def _bbox_size_deg(bbox: tuple[float, float, float, float]) -> float:
    """Smaller of (lat span, lon span) — used to gate subdivision."""
    min_lat, max_lat, min_lon, max_lon = bbox
    return min(max_lat - min_lat, max_lon - min_lon)


def _quarter(
    bbox: tuple[float, float, float, float],
) -> list[tuple[float, float, float, float]]:
    """Split a bbox into four equal quadrants (SW, SE, NW, NE)."""
    min_lat, max_lat, min_lon, max_lon = bbox
    mid_lat = (min_lat + max_lat) / 2
    mid_lon = (min_lon + max_lon) / 2
    return [
        (min_lat, mid_lat, min_lon, mid_lon),  # SW
        (min_lat, mid_lat, mid_lon, max_lon),  # SE
        (mid_lat, max_lat, min_lon, mid_lon),  # NW
        (mid_lat, max_lat, mid_lon, max_lon),  # NE
    ]


def _feature_oid(feat: dict) -> object:
    """Stable identity for dedup. Falls back to geometry hash if no OBJECTID."""
    props = feat.get("properties") or {}
    # ENC features expose OBJECTID in properties; ArcGIS may also use
    # FID or fid depending on serializer. Try the common spellings.
    for key in ("OBJECTID", "objectid", "FID", "fid"):
        if key in props and props[key] is not None:
            return (key, props[key])
    # No stable ID — geometry-hash fallback so identical adjacent
    # features at chunk boundaries still dedup.
    geom = feat.get("geometry")
    if geom:
        try:
            return ("geom", json.dumps(geom, sort_keys=True))
        except (TypeError, ValueError):
            pass
    return ("obj", id(feat))


def query_layer_geojson(
    service: str,
    layer_id: int,
    bbox: tuple[float, float, float, float],
) -> dict:
    """Hit one ENC Direct layer's /query endpoint. Auto-chunks on timeout.

    Strategy: try the full bbox first. If we hit a network/socket
    timeout, recursively split the bbox into quadrants and retry. Stops
    subdividing at MIN_CHUNK_DEG (~10 nm); raises if even the smallest
    chunk times out (very likely a real server problem at that point).

    Returns a FeatureCollection. Dedup runs whenever results are merged
    from sub-chunks because boundary polygons are returned by both
    neighbouring queries.
    """
    try:
        features = _query_once(service, layer_id, bbox)
        return {"type": "FeatureCollection", "features": features}
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        if _bbox_size_deg(bbox) < MIN_CHUNK_DEG:
            raise RuntimeError(
                f"timeout at minimum chunk size for {service} "
                f"layer {layer_id}: {exc}"
            ) from exc

        log.info("  layer %s: timeout on bbox %s, chunking 2x2",
                 layer_id, bbox)

        merged: list[dict] = []
        seen: set = set()
        for sub in _quarter(bbox):
            sub_fc = query_layer_geojson(service, layer_id, sub)
            for feat in sub_fc.get("features", []):
                oid = _feature_oid(feat)
                if oid in seen:
                    continue
                seen.add(oid)
                merged.append(feat)
            time.sleep(LAYER_SLEEP_S)

        return {"type": "FeatureCollection", "features": merged}


# ─── Pipeline ────────────────────────────────────────────────────────────


def merge_layers(
    service: str,
    bbox: tuple[float, float, float, float],
    layers: list[tuple[int, str]],
) -> dict:
    """Query each hazard layer, merge into one FeatureCollection.

    Annotates each feature with its source layer name in
    ``properties.layer`` so the API can report which layers contributed
    and the chart loader can apply the SKIP_LAYERS filter.
    """
    merged: list[dict] = []
    layer_counts: dict[str, int] = {}

    for layer_id, layer_name in layers:
        log.info("querying %s layer %s (%s)", service, layer_id, layer_name)
        try:
            fc = query_layer_geojson(service, layer_id, bbox)
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
        raise SystemExit(
            f"unknown region {region_name!r}. valid: {sorted(REGIONS)}"
        )

    region = REGIONS[region_name]
    service, layers = _service_for(region)
    log.info("ingesting region=%s kind=%s service=%s",
             region.name, region.kind, service)

    fc = merge_layers(service, region.bbox, layers)
    n = len(fc["features"])
    log.info("merged %s features across %s layers", n, len(layers))

    blob = json.dumps(fc).encode("utf-8")
    log.info("serialized GeoJSON: %.1f MB", len(blob) / 1e6)

    if dry_run:
        out_path = (
            Path(tempfile.gettempdir()) / "sailline_enc"
            / f"{region.name}_hazards.geojson"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(blob)
        log.info("dry-run: wrote %s", out_path)
        return {
            "region": region.name,
            "service": service,
            "feature_count": n,
            "size_bytes": len(blob),
            "local_path": str(out_path),
            "layer_counts": fc.get("_layer_counts", {}),
        }

    uri = upload_to_gcs(blob, region.name)
    return {
        "region": region.name,
        "service": service,
        "feature_count": n,
        "size_bytes": len(blob),
        "uri": uri,
        "layer_counts": fc.get("_layer_counts", {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest NOAA ENC hazard polygons to GCS"
    )
    parser.add_argument(
        "--region", required=True,
        help=f"region from app.regions ({sorted(REGIONS)})",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = ingest(args.region, dry_run=args.dry_run)
    log.info("done: %s", result)


if __name__ == "__main__":
    main()
