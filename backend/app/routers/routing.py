# backend/app/routers/routing.py
"""Route compute endpoint.

POST /api/routing/compute
    Body: { "race_id": "<uuid>", "safety_factor": 1.5,
            "duration_hours": 6.0 }   # optional; defaults via forecast_loader

Resolves the race, picks the wind region from marks centroid, builds a
time-aware WindForecast spanning the race window, runs the isochrone
engine threading simulated time, and returns a GeoJSON Feature plus
diagnostic metadata.

Region resolution returns a (base_region, venue) pair. base_region drives
wind + bathymetry lookup (always set; defaults to 'conus'). venue is set
only when the marks centroid falls inside a high-res venue bbox — that's
the trigger to load harbour-scale ENC hazards alongside the base ones.

Forecast not yet available: returns HTTP 425 (Too Early) with
{ available_at, hours_until_available }. The frontend schedules a
refetch at that timestamp.

Cache key: (engine_version, race_id, safety_factor, hrrr_cycle, gfs_cycle,
race_start_iso). race_start changes ⇒ cache miss; new cycle ⇒ cache miss.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import db, redis_client
from app.auth import get_current_user
from app.regions import REGIONS, base_region_for_point, venue_for_point
from app.services.bathymetry import BathymetryUnavailable
from app.services.boats import spec_for_class
from app.services.polars import load_polar
from app.services.routing import (
    DEFAULT_SAFETY_FACTOR,
    compute_isochrone_route,
    make_navigable_predicate,
    route_to_geojson,
)
from app.services.weather import ForecastNotAvailable, load_forecast_for_race

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/routing", tags=["routing"])


# ─── Models ──────────────────────────────────────────────────────────────


class ComputeRouteIn(BaseModel):
    race_id: UUID
    safety_factor: float = Field(default=DEFAULT_SAFETY_FACTOR, ge=1.0, le=3.0)
    duration_hours: float = Field(
        default=6.0, ge=0.5, le=240.0,
        description="How far past race_start to load forecast snapshots. "
                    "Defaults to 6h (covers most inshore/distance races); "
                    "set to ~50 for a Mac.",
    )


class RouteMeta(BaseModel):
    total_minutes: float
    tack_count: int
    reached: bool
    iterations: int
    nodes_explored: int
    region: str
    venue: Optional[str] = None
    forecast_quality: str            # "hrrr", "gfs", "hrrr+gfs"
    race_start: Optional[str]
    polar: str
    boat_class: str
    draft_m: float
    min_depth_m: float
    cached: bool


class ComputeRouteOut(BaseModel):
    route: dict
    meta: RouteMeta


class ForecastPendingOut(BaseModel):
    detail: str
    available_at: str
    hours_until_available: float


# ─── Helpers ─────────────────────────────────────────────────────────────


# Bump on any change to engine inputs/outputs (polar, mask, algorithm).
ENGINE_VERSION = "v8-segment-sampling"

ROUTE_CACHE_TTL_S = 3600


def _resolve_region(marks: list[dict]) -> tuple[str, Optional[str]]:
    """Return (base_region, venue_or_None) for the centroid of the marks.

    Base region drives wind + bathymetry lookup (always set, defaults to
    'conus'). Venue is set only when the centroid falls inside one of
    the high-res venue bboxes — that's the trigger for loading
    harbour-scale ENC hazards.
    """
    if not marks:
        return "conus", None
    lat_c = sum(m["lat"] for m in marks) / len(marks)
    lon_c = sum(m["lon"] for m in marks) / len(marks)
    base = base_region_for_point(lat_c, lon_c)
    venue = venue_for_point(lat_c, lon_c)
    return (
        base.name if base is not None else "conus",
        venue.name if venue is not None else None,
    )


async def _assert_race_owned(
    conn: asyncpg.Connection, race_id: UUID, uid: str,
) -> dict:
    row = await conn.fetchrow(
        """
        SELECT id, marks, boat_class, start_at
        FROM race_sessions
        WHERE id = $1 AND user_id = $2
        """,
        race_id, uid,
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
        "start_at": row["start_at"],
    }


# ─── Endpoint ────────────────────────────────────────────────────────────


@router.post("/compute", response_model=ComputeRouteOut,
             responses={425: {"model": ForecastPendingOut}})
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

    # Race start: use scheduled start_at; fall back to "now" for races
    # without a gun time set (the user is exploring routing pre-schedule).
    race_start = race["start_at"] or datetime.now(timezone.utc)
    if race_start.tzinfo is None:
        race_start = race_start.replace(tzinfo=timezone.utc)

    region, venue = _resolve_region(marks)
    if region not in REGIONS:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"resolved region {region!r} not in registry",
        )

    log.warning(
        "ROUTING DEBUG region=%s venue=%s has_gfs=%s race_start=%s now+18h=%s",
        region, venue,
        "gfs" in REGIONS[region].sources,
        race_start.isoformat(),
        (datetime.now(timezone.utc) + timedelta(hours=18)).isoformat(),
    )

    spec = spec_for_class(race["boat_class"])
    polar_path = f"app/services/polars/{spec.polar_csv}"
    polar = load_polar(polar_path)
    min_depth_m = spec.draft_m * payload.safety_factor

    # Load the forecast first — we need cycle ids for the cache key.
    try:
        forecast = await load_forecast_for_race(
            region=region,
            race_start=race_start,
            duration_hours=payload.duration_hours,
        )
    except ForecastNotAvailable as exc:
        raise HTTPException(
            status_code=425,  # Too Early
            detail={
                "detail": str(exc),
                "available_at": exc.available_at.isoformat(),
                "hours_until_available": exc.hours_until_available,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))

    # Cache key. Cycle iso is stable for a given cycle; race_start changes
    # per scheduled gun time. Forecast quality string captures whether
    # this is HRRR-only, GFS-only, or hybrid — not strictly needed for
    # correctness but it disambiguates routes computed against different
    # forecast horizons even within the same cycle. Venue is part of the
    # key so a venue-hazard ingest invalidates cached routes for that
    # venue without touching base-region routes.
    redis = redis_client.get_client()
    snapshot_sources = "+".join(
        sorted({s.source or "?" for s in forecast.snapshots})
    )
    cache_key = (
        f"route:{ENGINE_VERSION}:{payload.race_id}:"
        f"{race_start.isoformat()}:"
        f"{forecast.snapshots[0].reference_time}:{forecast.snapshots[-1].valid_time}:"
        f"{snapshot_sources}:{payload.safety_factor:.2f}:venue={venue or '-'}"
    )
    cached_blob = await redis.get(cache_key)
    if cached_blob is not None:
        cached = json.loads(cached_blob)
        cached["meta"]["cached"] = True
        log.info("route cache hit race_id=%s", payload.race_id)
        return cached

    try:
        is_navigable = make_navigable_predicate(
            region=region,
            draft_m=spec.draft_m,
            safety_factor=payload.safety_factor,
            venue=venue,
        )
    except BathymetryUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{exc}. Run bathymetry_ingest for this region before computing routes.",
        )

    start = (marks[0]["lat"], marks[0]["lon"])
    finish = (marks[-1]["lat"], marks[-1]["lon"])

    log.info(
        "compute route race_id=%s region=%s venue=%s polar=%s race_start=%s "
        "forecast_quality=%s start=%s finish=%s",
        payload.race_id, region, venue, polar.name, race_start.isoformat(),
        forecast.quality, start, finish,
    )

    result = compute_isochrone_route(
        start=start,
        finish=finish,
        polar=polar,
        wind=forecast,
        is_navigable=is_navigable,
        race_start=race_start,
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
            "venue": venue,
            "race_start": race_start.isoformat(),
            "forecast_quality": forecast.quality,
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
            "venue": venue,
            "forecast_quality": forecast.quality,
            "race_start": race_start.isoformat(),
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
