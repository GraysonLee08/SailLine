# backend/app/routers/ais.py
"""AIS read endpoint.

GET /api/ais?min_lat=&max_lat=&min_lon=&max_lon=

Returns the latest cached AIS positions inside the requested bounding
box. The cache is populated by ``workers/ais_subscribe.py``; this
endpoint only reads — it never opens a WebSocket of its own.

Design intent:
  * v1: display-only. The frontend overlays vessel sprites on the map.
    Engine integration (avoidance corridors) is parked for v2.
  * The endpoint is auth-gated like everything else; positions come
    from a paid third-party feed and we don't want anonymous scraping.
  * Default cap of 500 vessels per response — well above any realistic
    race-area population. Larger requests are clamped, not rejected.
  * Stale = absent. Cached entries TTL after 10 min (see services/ais.py)
    so a 200 with [] means "subscription is online but no vessels in
    this area," not "subscription is dead."
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app import redis_client
from app.auth import get_current_user
from app.services.ais import AisPosition, read_positions_in_bbox

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ais", tags=["ais"])


class AisPositionOut(BaseModel):
    mmsi: int
    lat: float
    lon: float
    sog_kts: Optional[float] = None
    cog_deg: Optional[float] = None
    heading_deg: Optional[float] = None
    name: Optional[str] = None
    ship_type: Optional[int] = None
    last_seen_ts: float


class AisListOut(BaseModel):
    bbox: list[float]   # [min_lat, max_lat, min_lon, max_lon]
    count: int
    vessels: list[AisPositionOut]


# Hard ceiling on returned vessels. Caps response size even when the
# client passes limit=99999.
ABSOLUTE_LIMIT = 500


@router.get("", response_model=AisListOut)
async def list_positions(
    min_lat: float = Query(..., ge=-90.0, le=90.0),
    max_lat: float = Query(..., ge=-90.0, le=90.0),
    min_lon: float = Query(..., ge=-180.0, le=180.0),
    max_lon: float = Query(..., ge=-180.0, le=180.0),
    limit: int = Query(ABSOLUTE_LIMIT, ge=1, le=ABSOLUTE_LIMIT),
    _user: dict = Depends(get_current_user),
) -> AisListOut:
    if min_lat >= max_lat:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "min_lat must be < max_lat",
        )
    if min_lon >= max_lon:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "min_lon must be < max_lon",
        )

    redis = redis_client.get_client()
    if redis is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AIS cache backend unavailable",
        )

    positions: list[AisPosition] = await read_positions_in_bbox(
        redis,
        min_lat=min_lat, max_lat=max_lat,
        min_lon=min_lon, max_lon=max_lon,
        limit=limit,
    )

    return AisListOut(
        bbox=[min_lat, max_lat, min_lon, max_lon],
        count=len(positions),
        vessels=[AisPositionOut(**p.to_dict()) for p in positions],
    )
