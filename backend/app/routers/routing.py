"""Route compute endpoint.

POST /api/routing/compute
    Body: { "race_id": "<uuid>" }

Resolves the race, picks the wind region from the marks centroid, reads
the latest HRRR grid from Redis, runs the isochrone engine, and returns a
GeoJSON Feature plus diagnostic metadata.

Caching: results are keyed by (race_id, wind_reference_time) and cached
in Redis for 1 hour. The wind reference_time changes hourly with the HRRR
cycle, so the cache naturally invalidates as fresh forecasts arrive.

Boat class → polar: v1 ships only the Beneteau First 36.7 polar. Other
boat classes route through the same polar with a warning log — the plan
explicitly cuts class-aware logic for Saturday.
"""
from __future__ import annotations

import gzip
import json
import logging
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import db, redis_client
from app.auth import get_current_user
from app.regions import REGIONS, base_region_for_point, venue_for_point
from app.services.polars import (
    BOAT_POLARS,
    DEFAULT_POLAR,
    load_polar_for_class,
)
from app.services.routing import (
    WindField,
    compute_isochrone_route,
    route_to_geojson,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/routing", tags=["routing"])


# ─── Models ──────────────────────────────────────────────────────────────


class ComputeRouteIn(BaseModel):
    race_id: UUID


class RouteMeta(BaseModel):
    total_minutes: float
    tack_count: int
    reached: bool
    iterations: int
    nodes_explored: int
    region: str
    wind_reference_time: Optional[str] = None
    wind_valid_time: Optional[str] = None
    polar: str
    cached: bool


class ComputeRouteOut(BaseModel):
    route: dict       # GeoJSON Feature (LineString)
    meta: RouteMeta


# ─── Helpers ─────────────────────────────────────────────────────────────


ROUTE_CACHE_TTL_S = 3600


def _resolve_region(marks: list[dict]) -> str:
    """Pick the wind region that covers the route.

    Preference order:
      1. Base region containing the marks centroid (covers the full path).
      2. CONUS as the catch-all fallback.

    Venues (high-res) are intentionally NOT preferred for routing — the
    engine sweeps far off the rhumb during long passages and most venue
    bboxes are too tight to hold the search frontier.
    """
    if not marks:
        return "conus"
    lat_c = sum(m["lat"] for m in marks) / len(marks)
    lon_c = sum(m["lon"] for m in marks) / len(marks)
    base = base_region_for_point(lat_c, lon_c)
    if base is not None:
        return base.name
    return "conus"


async def _read_wind_payload(region: str) -> dict:
    """Pull the gzipped HRRR JSON for `region` from Redis and decompress."""
    redis = redis_client.get_client()
    key = f"weather:hrrr:{region}:latest"
    blob = await redis.get(key)
    if blob is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"no wind data for region={region}",
        )
    raw = gzip.decompress(blob)
    return json.loads(raw)


async def _assert_race_owned(
    conn: asyncpg.Connection, race_id: UUID, uid: str
) -> dict:
    """Return the race row (id, marks, boat_class) or 404."""
    row = await conn.fetchrow(
        """
        SELECT id, marks, boat_class
        FROM race_sessions
        WHERE id = $1 AND user_id = $2
        """,
        race_id,
        uid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    marks_raw = row["marks"]
    if isinstance(marks_raw, (bytes, str)):
        marks = json.loads(marks_raw)
    else:
        marks = marks_raw or []
    return {
        "id": row["id"],
        "marks": marks,
        "boat_class": row["boat_class"],
    }


# ─── Endpoint ────────────────────────────────────────────────────────────


@router.post("/compute", response_model=ComputeRouteOut)
async def compute_route(
    payload: ComputeRouteIn,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    async with pool.acquire() as conn:
        race = await _assert_race_owned(conn, payload.race_id, user["uid"])

    marks = race["marks"]
    if len(marks) < 2:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "race must have at least 2 marks (start + finish)",
        )

    region = _resolve_region(marks)
    if region not in REGIONS:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"resolved region {region!r} not in registry",
        )

    wind_payload = await _read_wind_payload(region)
    wind = WindField.from_payload(wind_payload)

    redis = redis_client.get_client()
    ref_time = wind_payload.get("reference_time", "unknown")
    cache_key = f"route:{payload.race_id}:{ref_time}"

    cached_blob = await redis.get(cache_key)
    if cached_blob is not None:
        cached = json.loads(cached_blob)
        cached["meta"]["cached"] = True
        log.info("route cache hit race_id=%s ref=%s", payload.race_id, ref_time)
        return cached

    # Boat class → polar. v1 only has 36.7; everything else falls back.
    boat_class = race["boat_class"]
    if boat_class not in BOAT_POLARS:
        log.warning(
            "no polar for boat_class=%r; using %s",
            boat_class, DEFAULT_POLAR,
        )
    polar = load_polar_for_class(boat_class)

    # Treat the first and last marks as start and finish. Multi-leg
    # routing across waypoints comes in a later sprint.
    start = (marks[0]["lat"], marks[0]["lon"])
    finish = (marks[-1]["lat"], marks[-1]["lon"])

    log.info(
        "compute route race_id=%s region=%s polar=%s start=%s finish=%s",
        payload.race_id, region, polar.name, start, finish,
    )

    result = compute_isochrone_route(start=start, finish=finish, polar=polar, wind=wind)

    feature = route_to_geojson(
        result,
        properties={
            "start": list(start),
            "finish": list(finish),
            "polar": polar.name,
            "region": region,
            "wind_reference_time": wind.reference_time,
            "wind_valid_time": wind.valid_time,
        },
    )

    response: dict = {
        "route": feature,
        "meta": {
            "total_minutes": result.total_minutes,
            "tack_count": result.tack_count,
            "reached": result.reached,
            "iterations": result.iterations,
            "nodes_explored": result.nodes_explored,
            "region": region,
            "wind_reference_time": wind.reference_time,
            "wind_valid_time": wind.valid_time,
            "polar": polar.name,
            "cached": False,
        },
    }

    # Cache. Best-effort — a redis SET failure shouldn't fail the request.
    try:
        await redis.setex(cache_key, ROUTE_CACHE_TTL_S, json.dumps(response))
    except Exception as exc:  # noqa: BLE001
        log.warning("route cache write failed: %s", exc)

    return response
