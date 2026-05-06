"""Route compute endpoint.

POST /api/routing/compute
    Body: { "race_id": "<uuid>", "safety_factor": 1.5 }

Resolves the race, picks the wind region from the marks centroid, reads
the latest HRRR grid from Redis, builds a navigability predicate from
the boat's draft + region bathymetry + ENC hazards, runs the isochrone
engine, and returns a GeoJSON Feature plus diagnostic metadata.

Caching: results are keyed by (engine_version, race_id, wind_reference_time,
safety_factor) and cached in Redis for 1 hour. The engine version prefix
means a deploy that changes routing behavior automatically invalidates
old cached results without needing to flush Redis.

Failure modes:
    - Region has no ingested bathymetry → 503 with clear message
      ("run bathymetry_ingest for region=X"). Better than silently
      routing through land.
    - Region has bathymetry but no ENC charts → routes with depth-only.
      Still safe; ENC is additive.
"""
from __future__ import annotations

import gzip
import json
import logging
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import db, redis_client
from app.auth import get_current_user
from app.regions import REGIONS, base_region_for_point
from app.services.bathymetry import BathymetryUnavailable
from app.services.boats import spec_for_class
from app.services.polars import load_polar
from app.services.routing import (
    DEFAULT_SAFETY_FACTOR,
    WindField,
    compute_isochrone_route,
    make_navigable_predicate,
    route_to_geojson,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/routing", tags=["routing"])


# ─── Models ──────────────────────────────────────────────────────────────


class ComputeRouteIn(BaseModel):
    race_id: UUID
    safety_factor: float = Field(
        default=DEFAULT_SAFETY_FACTOR,
        ge=1.0,
        le=3.0,
        description="Multiplier on draft to compute minimum-safe depth. "
                    "1.5 default; 1.2 for racing in calm water, 2.0 for "
                    "buoy courses in shallow venues.",
    )


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
    boat_class: str
    draft_m: float
    min_depth_m: float
    cached: bool


class ComputeRouteOut(BaseModel):
    route: dict       # GeoJSON Feature (LineString)
    meta: RouteMeta


# ─── Helpers ─────────────────────────────────────────────────────────────


# Bump on any change to engine inputs/outputs (polar, mask, algorithm).
ENGINE_VERSION = "v5-finish-bin"   # was "v4-multileg"

ROUTE_CACHE_TTL_S = 3600


def _resolve_region(marks: list[dict]) -> str:
    """Pick the wind/bathy region that covers the route.

    Uses the centroid of the marks against the base region registry.
    Defaults to CONUS for marks that don't fall in a known region.
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

    # Boat spec drives polar + draft.
    spec = spec_for_class(race["boat_class"])
    polar_path = f"app/services/polars/{spec.polar_csv}"
    polar = load_polar(polar_path)
    min_depth_m = spec.draft_m * payload.safety_factor

    # Wind comes from Redis (already-cached HRRR cycle).
    wind_payload = await _read_wind_payload(region)
    wind = WindField.from_payload(wind_payload)

    # Cache key includes safety_factor — different drafts/factors get
    # different routes for the same race + wind cycle.
    redis = redis_client.get_client()
    ref_time = wind_payload.get("reference_time", "unknown")
    cache_key = (
        f"route:{ENGINE_VERSION}:{payload.race_id}:{ref_time}:{payload.safety_factor:.2f}"
    )

    cached_blob = await redis.get(cache_key)
    if cached_blob is not None:
        cached = json.loads(cached_blob)
        cached["meta"]["cached"] = True
        log.info("route cache hit race_id=%s ref=%s", payload.race_id, ref_time)
        return cached

    # Build navigability predicate. Failure here = no bathy ingested.
    try:
        is_navigable = make_navigable_predicate(
            region=region,
            draft_m=spec.draft_m,
            safety_factor=payload.safety_factor,
        )
    except BathymetryUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{exc}. Run bathymetry_ingest for this region before computing routes.",
        )

    start = (marks[0]["lat"], marks[0]["lon"])
    finish = (marks[-1]["lat"], marks[-1]["lon"])

    log.info(
        "compute route race_id=%s region=%s polar=%s draft=%.2fm "
        "min_depth=%.2fm start=%s finish=%s",
        payload.race_id, region, polar.name, spec.draft_m, min_depth_m,
        start, finish,
    )

    result = compute_isochrone_route(
        start=start,
        finish=finish,
        polar=polar,
        wind=wind,
        is_navigable=is_navigable,
    )

    feature = route_to_geojson(
        result,
        properties={
            "start": list(start),
            "finish": list(finish),
            "polar": polar.name,
            "boat_class": spec.name,
            "draft_m": spec.draft_m,
            "min_depth_m": min_depth_m,
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
            "boat_class": spec.name,
            "draft_m": spec.draft_m,
            "min_depth_m": min_depth_m,
            "cached": False,
        },
    }

    try:
        await redis.setex(cache_key, ROUTE_CACHE_TTL_S, json.dumps(response))
    except Exception as exc:  # noqa: BLE001
        log.warning("route cache write failed: %s", exc)

    return response
