"""Weather forecast read endpoint.

Region-keyed: each (source, region) pair maps to one Redis key whose value is
the full pre-clipped, gzipped JSON wind grid that the ingest worker wrote.
No bbox slicing — boats download the regional grid once and operate offline.

CDN-friendly: same URL → same response → cacheable. ETag + If-None-Match
gives reconnecting clients a zero-byte 304 when the cycle hasn't rotated.
"""
from __future__ import annotations

import hashlib
import logging
from asyncio import to_thread

from fastapi import APIRouter, HTTPException, Request, Response, status
from google.cloud import storage

from app import redis_client
from app.config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/weather", tags=["weather"])

# Each region is a pre-clipped grid produced by an ingest worker. Adding a
# region means registering it here AND deploying a worker that writes the
# corresponding region-scoped Redis key. Today both sources are clipped to
# great_lakes at ingest, so it's the only valid value.
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "great_lakes": (40.0, 50.0, -94.0, -75.0),
    # "chesapeake": (36.5, 39.5, -77.5, -75.5),
    # "sf_bay":     (37.0, 38.5, -123.0, -121.5),
}

SOURCES = ("hrrr", "gfs")

# Short enough to refresh inside one ingest cycle (HRRR is hourly), long
# enough that a fleet hitting the same URL collapses into one origin request.
CACHE_CONTROL = "public, max-age=300"


@router.get("")
async def get_weather(region: str, request: Request, source: str = "hrrr") -> Response:
    if region not in REGIONS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown region: {region}. valid: {sorted(REGIONS)}",
        )
    if source not in SOURCES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown source: {source}. valid: {list(SOURCES)}",
        )

    # Today every source is region-scoped at ingest to a single region
    # (great_lakes). When a second region ships, the worker will write to
    # weather:{source}:{region}:latest and this read path picks it up.
    key = f"weather:{source}:latest"

    blob = await _read_redis(key)
    if blob is None:
        log.warning("redis miss on %s, falling back to GCS", key)
        blob = await to_thread(_read_latest_gcs, source)
    if blob is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"no cached weather for source={source}",
        )

    # Hash of the stored bytes — changes iff the cycle rotated. Avoids
    # decompressing just to read reference_time out of the JSON body.
    etag = f'"{hashlib.sha256(blob).hexdigest()[:16]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    return Response(
        content=blob,
        media_type="application/json",
        headers={
            "Content-Encoding": "gzip",
            "Cache-Control": CACHE_CONTROL,
            "ETag": etag,
            "Vary": "Accept-Encoding",
        },
    )


async def _read_redis(key: str) -> bytes | None:
    try:
        client = redis_client.get_client()
    except HTTPException:
        return None
    try:
        return await client.get(key)
    except Exception:
        log.exception("redis GET failed for %s", key)
        return None


def _read_latest_gcs(source: str) -> bytes | None:
    """Sync — list/download from GCS. Call via asyncio.to_thread."""
    if not settings.gcs_weather_bucket:
        return None
    try:
        client = storage.Client()
        bucket = client.bucket(settings.gcs_weather_bucket)
        # Filenames are YYYYMMDDTHHMMZ.json.gz under {source}/, which sort
        # lexicographically by recency.
        blobs = sorted(
            bucket.list_blobs(prefix=f"{source}/"),
            key=lambda b: b.name,
            reverse=True,
        )
        if not blobs:
            return None
        # raw_download=True keeps the bytes gzipped. Without it the GCS
        # client transparently decompresses because we set content_encoding
        # at upload time, and we'd be re-gzipping on the way out.
        return blobs[0].download_as_bytes(raw_download=True)
    except Exception:
        log.exception("GCS fallback failed for source=%s", source)
        return None