# backend/app/services/ais.py
"""AIS vessel position cache + AISStream.io subscription helpers.

Two pieces in one module:

  1. **Redis cache layer** (sync, thin) — read/write per-vessel position
     records keyed by MMSI. Used by the AIS read endpoint and (in the
     worker) by the message handler.

  2. **AISStream.io WebSocket client** (async, long-running) — connects
     to ``wss://stream.aisstream.io/v0/stream``, sends a subscription
     message with a list of bounding boxes, and parses PositionReport
     and ShipStaticData messages into normalized records.

This module is intentionally NOT wired into the main API process. AIS
needs a persistent WebSocket subscription which is incompatible with
Cloud Run's autoscale-to-zero default — the right shape is a separate
service with ``min_instances=1`` that runs ``workers/ais_subscribe.py``
and writes to the shared Redis. The API reads from Redis only.

Why the split:
  * The WS subscription cost is per-connection. Running it in every
    autoscaled API instance would multiply traffic and message volume.
  * AISStream.io has a soft cap on simultaneous connections per API
    key. One worker instance ⇒ predictable traffic + predictable cost.
  * Restarts of the API process must not restart the AIS subscription
    (would burn keys + cause data gaps).

Cache TTL is 10 minutes. AIS positions become stale fast — a vessel
moving at 10 kt travels ~1.5 nm in 10 minutes. Beyond that, displaying
the cached point is more misleading than helpful, so the key just
disappears.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Iterable, Optional

import redis.asyncio as aioredis

log = logging.getLogger(__name__)


# ─── Constants ──────────────────────────────────────────────────────────

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"

# AIS position TTL. Beyond this we drop the cached entry — stale enough
# that the displayed position is misleading.
POSITION_TTL_S = 10 * 60

# Redis key prefix. Keyed by MMSI string so we can SCAN all vessels and
# filter by bbox in Python (sufficient at <2 000 vessels per race area).
AIS_KEY_PREFIX = "ais:vessel:"


# ─── Data shape ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AisPosition:
    """Normalized vessel position record.

    AIS message fields are pulled in from PositionReport (movement) and
    ShipStaticData (identity). We merge them on MMSI so the cached
    record carries both ``name`` + ``ship_type`` (from static) and
    ``lat`` + ``lon`` + ``sog_kts`` + ``cog_deg`` (from position).

    ``last_seen_ts`` is unix epoch seconds; lets clients render a "last
    update" age without re-reading the TTL.
    """
    mmsi: int
    lat: float
    lon: float
    sog_kts: Optional[float] = None    # speed over ground in knots
    cog_deg: Optional[float] = None    # course over ground in degrees
    heading_deg: Optional[float] = None
    name: Optional[str] = None         # vessel name from static record
    ship_type: Optional[int] = None    # AIS ship type code
    last_seen_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AisPosition":
        # Defensive: keys may be missing from older cached blobs.
        return cls(
            mmsi=int(d["mmsi"]),
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            sog_kts=_maybe_float(d.get("sog_kts")),
            cog_deg=_maybe_float(d.get("cog_deg")),
            heading_deg=_maybe_float(d.get("heading_deg")),
            name=d.get("name"),
            ship_type=d.get("ship_type"),
            last_seen_ts=float(d.get("last_seen_ts", 0.0)),
        )


def _maybe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── Redis cache ────────────────────────────────────────────────────────


async def write_position(redis: aioredis.Redis, pos: AisPosition) -> None:
    """Upsert a vessel position with TTL."""
    key = f"{AIS_KEY_PREFIX}{pos.mmsi}"
    await redis.setex(key, POSITION_TTL_S, json.dumps(pos.to_dict()))


async def read_positions_in_bbox(
    redis: aioredis.Redis,
    min_lat: float, max_lat: float,
    min_lon: float, max_lon: float,
    limit: int = 500,
) -> list[AisPosition]:
    """Return cached positions inside a bbox.

    Uses SCAN to enumerate keys then filters in Python. Fine while the
    cache holds < ~2 000 entries — well above any realistic race-area
    population. If we ever cache for the whole CONUS, swap this for a
    GEO* set keyed by region.
    """
    positions: list[AisPosition] = []
    seen = 0
    async for key in redis.scan_iter(match=f"{AIS_KEY_PREFIX}*", count=200):
        blob = await redis.get(key)
        if blob is None:
            continue
        try:
            data = json.loads(blob)
            pos = AisPosition.from_dict(data)
        except (ValueError, KeyError, TypeError) as exc:
            log.debug("skipping malformed AIS entry %s: %s", key, exc)
            continue
        if (min_lat <= pos.lat <= max_lat
                and min_lon <= pos.lon <= max_lon):
            positions.append(pos)
            seen += 1
            if seen >= limit:
                break
    return positions


# ─── AISStream.io message parsing ───────────────────────────────────────


def _parse_position_report(msg: dict[str, Any]) -> Optional[AisPosition]:
    """Normalize an AISStream PositionReport into our record.

    AISStream message shape (abbreviated):
      { "MessageType": "PositionReport",
        "MetaData": { "MMSI": 366742700, "ShipName": "EXAMPLE",
                      "time_utc": "..." },
        "Message": { "PositionReport": {
            "Latitude": 41.88, "Longitude": -87.62,
            "Sog": 5.4, "Cog": 180.0, "TrueHeading": 178, ... } } }

    Returns None if required fields are missing or have sentinel values
    (AIS uses Cog=360.0 and TrueHeading=511 to mean "not available").
    """
    meta = msg.get("MetaData") or {}
    pr = (msg.get("Message") or {}).get("PositionReport") or {}
    mmsi = meta.get("MMSI") or pr.get("UserID")
    if mmsi is None:
        return None
    lat = pr.get("Latitude")
    lon = pr.get("Longitude")
    if lat is None or lon is None:
        return None
    # AIS lat/lon sentinels: lat=91, lon=181 mean "not available".
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None

    cog = pr.get("Cog")
    if cog is not None and (cog >= 360.0 or cog < 0.0):
        cog = None  # AIS "no course" sentinel

    heading = pr.get("TrueHeading")
    if heading is not None and (heading >= 511 or heading < 0):
        heading = None  # AIS "no heading" sentinel

    sog = pr.get("Sog")
    # SOG of 102.3 is "not available". Anything > 102 is sentinel territory.
    if sog is not None and sog >= 102.0:
        sog = None

    return AisPosition(
        mmsi=int(mmsi),
        lat=float(lat),
        lon=float(lon),
        sog_kts=_maybe_float(sog),
        cog_deg=_maybe_float(cog),
        heading_deg=_maybe_float(heading),
        name=meta.get("ShipName") or None,
        ship_type=None,
        last_seen_ts=time.time(),
    )


def _parse_ship_static(msg: dict[str, Any]) -> Optional[tuple[int, dict[str, Any]]]:
    """Pull (mmsi, {"name": ..., "ship_type": ...}) from ShipStaticData.

    Returns None on malformed input. The worker merges this into the
    most recent PositionReport for the same MMSI so the cached record
    carries both identity and motion.
    """
    meta = msg.get("MetaData") or {}
    static = (msg.get("Message") or {}).get("ShipStaticData") or {}
    mmsi = meta.get("MMSI") or static.get("UserID")
    if mmsi is None:
        return None
    return int(mmsi), {
        "name": static.get("Name") or meta.get("ShipName") or None,
        "ship_type": static.get("Type") or None,
    }


# ─── WebSocket client ───────────────────────────────────────────────────


def build_subscribe_message(
    api_key: str,
    bboxes: Iterable[tuple[float, float, float, float]],
    message_types: Iterable[str] = ("PositionReport", "ShipStaticData"),
) -> str:
    """Build the JSON payload sent right after the WS connects.

    bboxes are passed as ``(min_lat, max_lat, min_lon, max_lon)`` tuples
    (the format every other internal API uses). AISStream wants
    ``[[lat_min, lon_min], [lat_max, lon_max]]`` per bbox, so we
    transpose here.
    """
    bboxes_payload = [
        [[float(b[0]), float(b[2])], [float(b[1]), float(b[3])]]
        for b in bboxes
    ]
    return json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": bboxes_payload,
        "FilterMessageTypes": list(message_types),
    })


async def consume_messages(
    websocket,
    static_cache: Optional[dict[int, dict[str, Any]]] = None,
) -> AsyncIterator[AisPosition]:
    """Async iterator over normalized positions from an open WS.

    Merges ShipStaticData identity into PositionReport positions: when
    a ShipStaticData arrives, the name/type are remembered in
    ``static_cache`` keyed by MMSI; subsequent positions enrich from
    that cache. The cache is in-process only (worker memory) — Redis
    holds the merged record.

    Pass in an external ``static_cache`` dict if you need access to
    accumulated identity records (e.g. for testing or graceful restart).
    """
    if static_cache is None:
        static_cache = {}

    async for raw in websocket:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("ignoring non-JSON AIS frame")
            continue

        mtype = msg.get("MessageType")
        if mtype == "ShipStaticData":
            parsed = _parse_ship_static(msg)
            if parsed is not None:
                mmsi, identity = parsed
                static_cache[mmsi] = identity
            continue

        if mtype != "PositionReport":
            continue

        pos = _parse_position_report(msg)
        if pos is None:
            continue

        # Enrich with cached identity if available.
        ident = static_cache.get(pos.mmsi)
        if ident is not None:
            pos = AisPosition(
                mmsi=pos.mmsi,
                lat=pos.lat, lon=pos.lon,
                sog_kts=pos.sog_kts, cog_deg=pos.cog_deg,
                heading_deg=pos.heading_deg,
                name=pos.name or ident.get("name"),
                ship_type=pos.ship_type or ident.get("ship_type"),
                last_seen_ts=pos.last_seen_ts,
            )
        yield pos


async def run_subscription(
    api_key: str,
    bboxes: Iterable[tuple[float, float, float, float]],
    redis: aioredis.Redis,
    *,
    connect_factory=None,
    reconnect_backoff_s: float = 5.0,
    max_backoff_s: float = 60.0,
) -> None:
    """Long-running coroutine: connect, subscribe, cache positions forever.

    Reconnects on any failure with exponential backoff capped at 60 s.
    ``connect_factory`` lets tests inject a fake — production passes
    the real ``websockets.connect`` here.
    """
    if connect_factory is None:
        import websockets  # local import so tests can stub the package
        connect_factory = websockets.connect

    backoff = reconnect_backoff_s
    bboxes = list(bboxes)
    sub_msg = build_subscribe_message(api_key, bboxes)
    static_cache: dict[int, dict[str, Any]] = {}

    while True:
        try:
            async with connect_factory(AISSTREAM_URL) as ws:
                log.info("AIS subscription connected; bboxes=%d", len(bboxes))
                await ws.send(sub_msg)
                backoff = reconnect_backoff_s  # reset on successful connect
                async for pos in consume_messages(ws, static_cache=static_cache):
                    try:
                        await write_position(redis, pos)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("AIS cache write failed: %s", exc)
        except asyncio.CancelledError:
            log.info("AIS subscription cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "AIS subscription error: %s — reconnecting in %.1fs",
                exc, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, max_backoff_s)


__all__ = [
    "AisPosition",
    "AISSTREAM_URL",
    "POSITION_TTL_S",
    "AIS_KEY_PREFIX",
    "build_subscribe_message",
    "consume_messages",
    "read_positions_in_bbox",
    "run_subscription",
    "write_position",
]
