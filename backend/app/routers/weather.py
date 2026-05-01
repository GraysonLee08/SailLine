"""Weather forecast read endpoint.

Region-keyed: each (source, region) pair maps to one Redis key whose value is
the full pre-clipped, gzipped JSON wind grid that the ingest worker wrote.
No bbox slicing — boats download the regional grid once and operate offline.

CDN-friendly: same URL → same response → cacheable. ETag + If-None-Match
gives reconnecting clients a zero-byte 304 when the cycle hasn't rotated.

Region registry lives in app.regions — import from there, don't redefine.
"""
from __future__ import annotations

import hashlib
import logging
from asyncio import to_thread

from fastapi import APIRouter, HTTPException, Request, Response, status
from google.cloud import storage

from app import redis_client
from app.config import settings
from app.regions import REGIONS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/weather", tags=["weather"])

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
    region_obj = REGIONS[region]
    if source not in region_obj.sources:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"source {source!r} not available for region {region!r}. "
            f"valid: {list(region_obj.sources)}",
        )

    key = f"weather:{source}:{region}:latest"
    blob = await _read_redis(key)

    if blob is None:
        log.warning("redis miss on %s, falling back to GCS", key)
        blob = await to_thread(_read_latest_gcs, source, region)

    if blob is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"no cached weather for source={source} region={region}",
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


def _read_latest_gcs(source: str, region: str) -> bytes | None:
    """Sync — list/download from GCS. Call via asyncio.to_thread.

    Looks under {source}/{region}/. Filenames are YYYYMMDDTHHMMZ.json.gz
    so lexicographic sort == reverse-chronological.
    """
    if not settings.gcs_weather_bucket:
        return None
    try:
        client = storage.Client()
        bucket = client.bucket(settings.gcs_weather_bucket)

        prefix = f"{source}/{region}/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        if not blobs:
            return None
        blobs.sort(key=lambda b: b.name, reverse=True)

        # raw_download=True keeps the bytes gzipped. Without it the GCS
        # client transparently decompresses because we set content_encoding
        # at upload time, and we'd be re-gzipping on the way out.
        return blobs[0].download_as_bytes(raw_download=True)
    except Exception:
        log.exception("GCS fallback failed for source=%s region=%s", source, region)
        return None
