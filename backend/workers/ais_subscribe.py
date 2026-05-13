# backend/workers/ais_subscribe.py
"""AIS subscription worker.

Long-running process. Connects to AISStream.io over WebSocket,
subscribes to one or more bounding boxes, parses incoming PositionReport
and ShipStaticData messages, and upserts the latest position per MMSI
to Redis (TTL 10 min).

This is intentionally NOT part of the main FastAPI app: Cloud Run
autoscales API instances and would create N concurrent subscriptions,
multiplying traffic and burning AISStream rate limits. The right shape
is a dedicated Cloud Run service running this entrypoint with
``--min-instances=1 --max-instances=1`` and ``--no-cpu-throttling``,
behind the same VPC connector so it can write to Memorystore.

Usage (from backend/):
    AISSTREAM_API_KEY=xxx python -m workers.ais_subscribe \\
        --bbox 41.6,42.5,-88.0,-87.2 \\
        --bbox 42.7,43.4,-88.1,-87.5

By default the worker also subscribes to every venue listed in
``app/regions.py`` so the same process covers all the harbour-scale
race areas without needing per-venue bbox arguments.

Environment:
    AISSTREAM_API_KEY    — required.
    REDIS_HOST / PORT    — same env vars the main app uses (app.config).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from typing import Iterable, Optional

from app import redis_client
from app.regions import venue_regions
from app.services.ais import run_subscription

log = logging.getLogger(__name__)


def _parse_bbox(arg: str) -> tuple[float, float, float, float]:
    """Parse ``min_lat,max_lat,min_lon,max_lon`` from a CLI flag."""
    parts = [p.strip() for p in arg.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"bbox expects 4 comma-separated floats, got {arg!r}"
        )
    try:
        nums = tuple(float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"bbox not numeric: {exc}") from exc
    min_lat, max_lat, min_lon, max_lon = nums
    if min_lat >= max_lat:
        raise argparse.ArgumentTypeError(f"bbox min_lat >= max_lat in {arg!r}")
    if min_lon >= max_lon:
        raise argparse.ArgumentTypeError(f"bbox min_lon >= max_lon in {arg!r}")
    return (min_lat, max_lat, min_lon, max_lon)


def _default_bboxes() -> list[tuple[float, float, float, float]]:
    """Every configured venue's bbox.

    Keeps the worker zero-config in production: as new venues are added
    to ``app/regions.py`` they automatically get covered without a
    redeploy of this worker's launch command.
    """
    return [r.bbox for r in venue_regions()]


async def _main(
    bboxes: Iterable[tuple[float, float, float, float]],
    api_key: str,
) -> None:
    await redis_client.startup()
    redis = redis_client.get_client()
    if redis is None:
        raise RuntimeError("Redis client not initialized")

    stop = asyncio.Event()

    def _shutdown(*_a) -> None:
        log.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows fallback — Ctrl+C raises KeyboardInterrupt naturally.
            pass

    sub_task = asyncio.create_task(run_subscription(api_key, bboxes, redis))
    stop_task = asyncio.create_task(stop.wait())

    done, pending = await asyncio.wait(
        {sub_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    for t in done:
        # Surface any exception from the subscription coroutine.
        if t is sub_task and not t.cancelled():
            try:
                t.result()
            except Exception:  # noqa: BLE001
                log.exception("AIS subscription terminated")

    await redis_client.shutdown()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="AISStream.io subscription worker — caches positions to Redis.",
    )
    parser.add_argument(
        "--bbox", type=_parse_bbox, action="append",
        help="bbox to subscribe to (min_lat,max_lat,min_lon,max_lon). "
             "Repeat for multiple bboxes. Defaults to every venue in regions.py.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        raise SystemExit(
            "AISSTREAM_API_KEY not set. Get a key from https://aisstream.io "
            "and put it in Secret Manager / your local .env."
        )

    bboxes = args.bbox or _default_bboxes()
    if not bboxes:
        raise SystemExit("no bboxes resolved — pass --bbox or define venues.")

    log.info("starting AIS worker; bboxes=%d", len(bboxes))
    try:
        asyncio.run(_main(bboxes, api_key))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
