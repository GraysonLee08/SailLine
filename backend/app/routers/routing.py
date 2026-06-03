# backend/app/routers/routing.py
"""Route compute HTTP endpoint.

POST /api/routing/compute
    Body: see :class:`ComputeRouteIn`. Returns a GeoJSON Feature plus
    diagnostic meta, or HTTP 425 (Too Early) when the forecast for the
    race window hasn't been ingested yet.

This module is the *transport* layer for the routing pipeline. The
heavy lifting (region resolution, forecast + currents loading, cache
key, engine call, GeoJSON assembly) lives in
``app/services/routing/pipeline.py`` — same code path the background
recompute worker uses, so 'better route' alerts are computed against
the same physics the user sees.

Concerns kept here:

* Pydantic wire models for FastAPI validation.
* Race ownership check (auth concern — pipeline is auth-agnostic).
* Pro-tier polar gating: free-tier callers route with the GENERIC
  polar regardless of which boat_class the race carries.
* HTTP status mapping: ForecastNotAvailable → 425, ownership fail →
  404, marks < 2 → 400, missing bathymetry → 503.
* Persisting the user's request via ``pipeline.save_last_request`` so
  the background recompute worker can faithfully replay it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import db, redis_client
from app.auth import get_current_user
from app.services.bathymetry import BathymetryUnavailable
from app.services.routing import (
    DEFAULT_SAFETY_FACTOR,
    ENGINE_VERSION,
    DeratingProfile,
    RouteRequest,
    compute_route,
    save_last_request,
)
from app.services.weather import ForecastNotAvailable

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/routing", tags=["routing"])


# ─── Wire models ─────────────────────────────────────────────────────────


class ComputeRouteIn(BaseModel):
    race_id: UUID
    safety_factor: float = Field(default=DEFAULT_SAFETY_FACTOR, ge=1.0, le=3.0)
    duration_hours: float = Field(
        default=6.0, ge=0.5, le=240.0,
        description="How far past race_start to load forecast snapshots. "
                    "Defaults to 6h (covers most inshore/distance races); "
                    "set to ~50 for a Mac.",
    )
    max_tws_kt: Optional[float] = Field(
        default=None, ge=5.0, le=80.0,
        description="Heavy-weather cutoff in knots. Candidates from frontier "
                    "points where forecast TWS exceeds this are not expanded. "
                    "Null = no cutoff.",
    )
    polar_margin: float = Field(
        default=0.97, ge=0.5, le=1.0,
        description="Global multiplier on polar boat speeds. 1.0 = no margin; "
                    "0.97 (default) bakes in a conservative buffer for gust "
                    "variability and helm-skill vs. polar idealization.",
    )
    hs_m: float = Field(
        default=0.0, ge=0.0, le=10.0,
        description="Significant wave height in metres. Until the wave ingest "
                    "worker is online this is caller-supplied; the engine "
                    "applies an upwind penalty / downwind bonus accordingly.",
    )
    density_factor: float = Field(
        default=1.0, ge=0.8, le=1.2,
        description="Air density relative to standard (1.225 kg/m³). Cold "
                    "dense air → >1; hot humid air → <1. Scales effective "
                    "TWS by sqrt(density_factor).",
    )


class RouteMeta(BaseModel):
    total_minutes: float
    tack_count: int
    reached: bool
    iterations: int
    nodes_explored: int
    legs: int
    region: str
    venue: Optional[str] = None
    forecast_quality: str            # "hrrr", "gfs", "hrrr+gfs"
    race_start: Optional[str]
    polar: str
    boat_class: str
    draft_m: float
    min_depth_m: float
    cached: bool
    max_tws_kt: Optional[float] = None
    polar_margin: float = 1.0
    hs_m: float = 0.0
    density_factor: float = 1.0
    currents_quality: Optional[str] = None
    start_wind_dir_deg: Optional[float] = None
    start_wind_speed_kt: Optional[float] = None


class ComputeRouteOut(BaseModel):
    route: dict
    meta: RouteMeta


class ForecastPendingOut(BaseModel):
    detail: str
    available_at: str
    hours_until_available: float


# ─── Helpers ─────────────────────────────────────────────────────────────


async def _assert_race_owned(
    conn: asyncpg.Connection, race_id: UUID, uid: str,
) -> dict:
    """D3: route compute is a WRITE op (owner + crew). Viewer can READ
    the route the owner already computed via the SSE stream; they can't
    kick off a fresh compute."""
    from app.auth_helpers import race_write_predicate
    pred = race_write_predicate(race_alias="r", uid_placeholder="$2")
    row = await conn.fetchrow(
        f"""
        SELECT r.id, r.marks, r.boat_class, r.start_at
        FROM race_sessions r
        WHERE r.id = $1 AND {pred}
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


def _effective_boat_class(requested: str, tier: Optional[str]) -> str:
    """D3: free-tier callers route with the GENERIC polar regardless of
    which boat_class the race carries. Pro + Hardware tiers get the
    class-specific polar.
    """
    return requested if tier in ("pro", "hardware") else "GENERIC"


# ─── Endpoint ────────────────────────────────────────────────────────────


@router.post("/compute", response_model=ComputeRouteOut,
             responses={425: {"model": ForecastPendingOut}})
async def compute_route_endpoint(
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
    # without a gun time set (user exploring routing pre-schedule).
    race_start = race["start_at"] or datetime.now(timezone.utc)
    if race_start.tzinfo is None:
        race_start = race_start.replace(tzinfo=timezone.utc)

    req = RouteRequest(
        race_id=payload.race_id,
        marks=marks,
        race_start=race_start,
        boat_class=_effective_boat_class(race["boat_class"], user.get("tier")),
        safety_factor=payload.safety_factor,
        duration_hours=payload.duration_hours,
        derating=DeratingProfile(
            max_tws_kt=payload.max_tws_kt,
            polar_margin=payload.polar_margin,
            hs_m=payload.hs_m,
            density_factor=payload.density_factor,
        ),
    )

    redis = redis_client.get_client()

    try:
        outcome = await compute_route(req, redis=redis, use_cache=True)
    except ForecastNotAvailable as exc:
        raise HTTPException(
            status_code=425,  # Too Early
            detail={
                "detail": str(exc),
                "available_at": exc.available_at.isoformat(),
                "hours_until_available": exc.hours_until_available,
            },
        )
    except BathymetryUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{exc}. Run bathymetry_ingest for this region before computing routes.",
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))

    # Persist the user's tunables so the background recompute worker
    # replays the same physics. Non-fatal: a write failure is logged
    # inside save_last_request and the user's response still ships.
    await save_last_request(req, redis=redis)

    return {"route": outcome.feature, "meta": outcome.meta}
