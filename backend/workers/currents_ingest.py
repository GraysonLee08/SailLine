"""NOAA OFS currents ingestion -> native-grid u/v cache, per source + run type.

Production: runs as a Cloud Run Job per (source, run_type) pair — one for
each forecast cycle, one for each nowcast refresh. Scheduler triggers:

    forecast:  every 6h   (matches OFS cycle cadence)
    nowcast:   hourly     (refreshes the recent-past analyzed window)

Mirrors ``weather_ingest`` in shape; differs in two important ways:

1. **No bbox clipping or regridding.** OFS data is preserved on its
   native grid (FVCOM unstructured mesh, or ROMS/POM curvilinear
   structured) so shoreline fidelity around complex embayments is not
   lost. ``services.currents.netcdf_extract`` does the format parsing.

2. **Static topology cached separately.** The mesh (FVCOM) or grid
   (ROMS) is identical across every cycle for a given source. It is
   written once under ``currents:{source}:topology`` and reused; the
   per-fhour blobs carry only u, v, valid_time. Cuts the Redis footprint
   roughly 10x compared to embedding the topology in every snapshot.

Run types:

    forecast (``f``)  — files cover f000..f{forecast_horizon} forward
                        from cycle start. The routing engine consumes
                        these for the race window.
    nowcast  (``n``)  — files cover n001..n{nowcast_horizon} BACKWARD
                        from cycle start (analyzed conditions in the
                        recent past). Useful for the "current
                        conditions" overlay and for races that start
                        very near "now" where the most-recent nowcast
                        slice is fresher than the cycle's f000.

Usage (from backend/):

    python -m workers.currents_ingest lmhofs --dry-run
    python -m workers.currents_ingest lmhofs --run-type nowcast --dry-run
    python -m workers.currents_ingest cbofs --fhour 1 --dry-run

Cloud Run Job command (one per source per run_type):

    python -m workers.currents_ingest lmhofs --run-type forecast
    python -m workers.currents_ingest lmhofs --run-type nowcast

Redis key shape:

    currents:{source}:topology                              gzipped JSON, mesh / grid
    currents:{source}:{cycle}:{f|n}{fhour:03d}              gzipped JSON, u/v + valid_time
    currents:{source}:{cycle}:{f|n}_manifest                cycle metadata, one per run type
    currents:{source}:cycles                                sorted set, score = cycle epoch

GCS layout under ``GCS_CURRENTS_BUCKET``:

    {source}/topology.json.gz                               durability copy of topology
    {source}/{cycle}/{f|n}{fhour:03d}.json.gz               per-fhour archive
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import redis
from google.cloud import storage

from app.currents_regions import CURRENT_SOURCES, CurrentSource
from app.services.currents.netcdf_extract import (
    FvcomMesh,
    FvcomSnapshot,
    RomsGrid,
    RomsSnapshot,
    extract,
)

log = logging.getLogger(__name__)

RunType = Literal["f", "n"]

# Long TTL on topology — it's static per source. We refresh it whenever
# a worker finds it missing rather than expiring it on a schedule.
TOPOLOGY_TTL_SECONDS = 30 * 24 * 3600

# Per-cycle TTL on fhour blobs and manifests. Generous compared to the
# 6h cycle step so that a missed cycle doesn't strand routes; the cycles
# ZSET trim keeps memory bounded.
CYCLE_TTL_SECONDS = 12 * 3600

# Friendly aliases for the CLI.
_RUN_TYPE_ALIASES = {
    "forecast": "f",
    "nowcast": "n",
    "f": "f",
    "n": "n",
}


# ---------------------------------------------------------------------------
# Cycle resolution


def latest_cycle(source: CurrentSource) -> tuple[str, int]:
    """Most recent cycle expected to be fully published.

    Both forecast and nowcast files for a given cycle are published in
    the same NOMADS directory and become available together, so the
    publish-lag estimate is shared.
    """
    now = datetime.now(timezone.utc) - timedelta(hours=source.publish_lag_hours)
    cycle = (now.hour // source.cycle_step_hours) * source.cycle_step_hours
    return now.strftime("%Y%m%d"), cycle


# ---------------------------------------------------------------------------
# Download


def _urlopen_with_retries(req, *, timeout: int, max_attempts: int = 3):
    """HTTP open with exponential-backoff retries on 5xx / connection errors.

    Mirrors the helper in weather_ingest. Kept inline rather than shared
    to keep workers/* independent — they deploy separately.
    """
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == max_attempts:
                raise
            print(f"  retry {attempt}/{max_attempts} after HTTP {e.code}", flush=True)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == max_attempts:
                raise
            print(f"  retry {attempt}/{max_attempts} after {type(e).__name__}", flush=True)
        time.sleep(delay)
        delay *= 2


def download_netcdf(url: str, out: Path, *, timeout: int = 240) -> int:
    """Download a NetCDF file to ``out``. Returns bytes received.

    OFS NetCDFs are 20-100 MB each — well within a 4-minute fetch over
    Cloud Run's egress to NOMADS. Whole-file download (no byte ranges)
    because NetCDF doesn't have an idx-style sidecar; the savings would
    require server-side subsetting (NCSS) which not every OFS model
    publishes consistently.
    """
    req = urllib.request.Request(url)
    bytes_written = 0
    with _urlopen_with_retries(req, timeout=timeout) as resp, out.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 20)  # 1 MB
            if not chunk:
                break
            fh.write(chunk)
            bytes_written += len(chunk)
    return bytes_written


# ---------------------------------------------------------------------------
# Serialisation


def _serialize_topology(topology) -> bytes:
    """Encode the static mesh / grid as gzipped JSON.

    FVCOM: nodes (lat, lon) + triangle connectivity.
    ROMS:  2-D rho-grid lat, lon, mask, angle.

    Stored once per source under ``currents:{source}:topology`` and
    reused by every cycle. The field samplers know how to consume this
    payload via the loader.
    """
    if isinstance(topology, FvcomMesh):
        payload = {
            "kind": "fvcom",
            "source": topology.source,
            "n_nodes": topology.n_nodes,
            "n_triangles": topology.n_triangles,
            "lats": topology.lats.astype(np.float32).tolist(),
            "lons": topology.lons.astype(np.float32).tolist(),
            "triangles": topology.triangles.astype(np.int32).tolist(),
        }
    elif isinstance(topology, RomsGrid):
        payload = {
            "kind": "roms",
            "source": topology.source,
            "shape": list(topology.lats.shape),
            "lats": topology.lats.astype(np.float32).tolist(),
            "lons": topology.lons.astype(np.float32).tolist(),
            "mask": topology.mask.astype(bool).tolist(),
            "angle": topology.angle.astype(np.float32).tolist(),
        }
    else:
        raise TypeError(f"unknown topology type: {type(topology).__name__}")
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw)


def _serialize_snapshot(snapshot, run_type: RunType) -> bytes:
    """Encode one fhour of u, v as gzipped JSON.

    ``run_type`` is recorded inside the payload so the loader can
    distinguish nowcast vs forecast samples without re-parsing the key.
    """
    if isinstance(snapshot, FvcomSnapshot):
        payload = {
            "kind": "fvcom",
            "source": snapshot.source,
            "cycle": snapshot.cycle_iso,
            "run_type": run_type,
            "fhour": snapshot.fhour,
            "reference_time": snapshot.reference_time.isoformat(),
            "valid_time": snapshot.valid_time.isoformat(),
            "u": _to_finite_list(snapshot.u),
            "v": _to_finite_list(snapshot.v),
        }
    elif isinstance(snapshot, RomsSnapshot):
        payload = {
            "kind": "roms",
            "source": snapshot.source,
            "cycle": snapshot.cycle_iso,
            "run_type": run_type,
            "fhour": snapshot.fhour,
            "reference_time": snapshot.reference_time.isoformat(),
            "valid_time": snapshot.valid_time.isoformat(),
            "shape": list(snapshot.u.shape),
            "u": _to_finite_list(snapshot.u),
            "v": _to_finite_list(snapshot.v),
        }
    else:
        raise TypeError(f"unknown snapshot type: {type(snapshot).__name__}")
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw)


def _to_finite_list(arr: np.ndarray) -> list:
    """Convert numpy array to nested Python lists, replacing NaN/Inf with None.

    JSON has no representation for NaN; using None preserves the
    'masked / no-data' semantic the field samplers expect.
    """
    masked = np.where(np.isfinite(arr), arr, None)  # type: ignore[arg-type]
    return masked.tolist()


# ---------------------------------------------------------------------------
# Storage writes


def _redis_client() -> redis.Redis:
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=0,
    )


def _write_topology_redis(client: redis.Redis, source: str, blob: bytes) -> None:
    key = f"currents:{source}:topology"
    client.setex(key, TOPOLOGY_TTL_SECONDS, blob)


def _topology_exists_redis(client: redis.Redis, source: str) -> bool:
    return bool(client.exists(f"currents:{source}:topology"))


def _write_topology_gcs(source: str, blob: bytes) -> str:
    bucket_name = os.environ["GCS_CURRENTS_BUCKET"]
    gcs = storage.Client()
    bucket = gcs.bucket(bucket_name)
    archive_path = f"{source}/topology.json.gz"
    obj = bucket.blob(archive_path)
    obj.content_encoding = "gzip"
    obj.upload_from_string(blob, content_type="application/json")
    return f"gs://{bucket_name}/{archive_path}"


def _snapshot_redis_key(source: str, cycle_iso: str, run_type: RunType, fhour: int) -> str:
    return f"currents:{source}:{cycle_iso}:{run_type}{fhour:03d}"


def _manifest_redis_key(source: str, cycle_iso: str, run_type: RunType) -> str:
    return f"currents:{source}:{cycle_iso}:{run_type}_manifest"


def _write_snapshot_redis(
    client: redis.Redis, source: str, cycle_iso: str,
    run_type: RunType, fhour: int, blob: bytes,
) -> None:
    client.setex(
        _snapshot_redis_key(source, cycle_iso, run_type, fhour),
        CYCLE_TTL_SECONDS,
        blob,
    )


def _write_snapshot_gcs(
    source: str, cycle_iso: str, run_type: RunType, fhour: int, blob: bytes,
) -> str:
    bucket_name = os.environ["GCS_CURRENTS_BUCKET"]
    gcs = storage.Client()
    bucket = gcs.bucket(bucket_name)
    archive_path = f"{source}/{cycle_iso}/{run_type}{fhour:03d}.json.gz"
    obj = bucket.blob(archive_path)
    obj.content_encoding = "gzip"
    obj.upload_from_string(blob, content_type="application/json")
    return f"gs://{bucket_name}/{archive_path}"


# ---------------------------------------------------------------------------
# Core ingest


def _fetch_one(
    source: CurrentSource, run_type: RunType, fhour: int,
) -> tuple[object, object, str]:
    """Download + parse one fhour. Returns (topology, snapshot, cycle_iso)."""
    date, cycle = latest_cycle(source)
    url = source.url_for(run_type, date, cycle, fhour)
    tag = f"{source.name} {run_type}{fhour:03d}"
    print(f"[{tag}] cycle={date} {cycle:02d}Z url={url}", flush=True)

    fd, tmp_path_str = tempfile.mkstemp(suffix=".nc")
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    try:
        size = download_netcdf(url, tmp_path)
        print(f"[{tag}] downloaded {size / 1e6:.1f} MB", flush=True)
        topology, snapshot = extract(
            tmp_path, source=source.name, grid_type=source.grid_type, fhour=fhour,
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    return topology, snapshot, snapshot.cycle_iso  # type: ignore[union-attr]


def ingest(
    source_name: str,
    fhour: int,
    *,
    run_type: RunType = "f",
    dry_run: bool = False,
) -> dict:
    """Single-fhour ingest. Useful for ad-hoc backfills and testing."""
    source = _resolve(source_name)
    topology, snapshot, cycle_iso = _fetch_one(source, run_type, fhour)

    topo_blob = _serialize_topology(topology)
    snap_blob = _serialize_snapshot(snapshot, run_type)

    if dry_run:
        out_dir = Path(__file__).parent.parent / "ingest_output"
        out_dir.mkdir(exist_ok=True)
        (out_dir / f"currents_{source.name}_topology.json.gz").write_bytes(topo_blob)
        (out_dir / f"currents_{source.name}_{cycle_iso}_{run_type}{fhour:03d}.json.gz").write_bytes(snap_blob)
        print(f"[{source.name}] dry-run wrote topology + {run_type}{fhour:03d}", flush=True)
        return {"source": source.name, "cycle": cycle_iso, "run_type": run_type, "fhour": fhour}

    client = _redis_client()
    if not _topology_exists_redis(client, source.name):
        _write_topology_redis(client, source.name, topo_blob)
        _write_topology_gcs(source.name, topo_blob)
        print(f"[{source.name}] topology written", flush=True)
    _write_snapshot_redis(client, source.name, cycle_iso, run_type, fhour, snap_blob)
    _write_snapshot_gcs(source.name, cycle_iso, run_type, fhour, snap_blob)
    return {"source": source.name, "cycle": cycle_iso, "run_type": run_type, "fhour": fhour}


def ingest_cycle(
    source_name: str,
    *,
    run_type: RunType = "f",
    dry_run: bool = False,
) -> dict:
    """Ingest the full fhour sequence for the latest cycle of one run type.

    Forecast: walks f000..f{forecast_horizon}.
    Nowcast:  walks n001..n{nowcast_horizon}.

    Stops on the first 404. OFS publishes cycles in order, so within a
    run-type sequence a 404 means the cycle isn't fully out yet and
    trailing fhours will also be missing — keep what we have, return.

    Forecast and nowcast manifests are written under separate Redis keys
    so the two ingest schedules don't trample each other.
    """
    source = _resolve(source_name)
    fhours = source.fhour_range(run_type)

    print(
        f"[{source.name}] {run_type}-cycle ingest, "
        f"fhours={fhours[0]}..{fhours[-1]} "
        f"({len(fhours)} files, grid={source.grid_type})",
        flush=True,
    )

    cycle_iso: Optional[str] = None
    valid_times: dict[int, str] = {}
    topo_written = False
    topo_blob: Optional[bytes] = None
    snapshot_blobs: dict[int, bytes] = {}  # only kept in memory for dry-run

    client: Optional[redis.Redis] = None if dry_run else _redis_client()

    for fh in fhours:
        try:
            topology, snapshot, this_cycle_iso = _fetch_one(source, run_type, fh)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  fhour {run_type}{fh:03d}: 404 (not yet published) — stopping", flush=True)
                break
            raise
        cycle_iso = this_cycle_iso
        valid_times[fh] = snapshot.valid_time.isoformat()  # type: ignore[union-attr]

        # Topology — extract once, write once per worker invocation if
        # missing from Redis. Subsequent fhours short-circuit.
        if not topo_written:
            topo_blob = _serialize_topology(topology)
            if not dry_run:
                if not _topology_exists_redis(client, source.name):  # type: ignore[arg-type]
                    _write_topology_redis(client, source.name, topo_blob)  # type: ignore[arg-type]
                    _write_topology_gcs(source.name, topo_blob)
                    print(f"  topology written ({len(topo_blob) / 1e3:.1f} KB gz)", flush=True)
            topo_written = True

        snap_blob = _serialize_snapshot(snapshot, run_type)
        if dry_run:
            snapshot_blobs[fh] = snap_blob
            continue
        _write_snapshot_redis(client, source.name, cycle_iso, run_type, fh, snap_blob)  # type: ignore[arg-type]
        _write_snapshot_gcs(source.name, cycle_iso, run_type, fh, snap_blob)

    if cycle_iso is None:
        raise RuntimeError(
            f"no fhours ingested for {source.name} {run_type}-cycle — "
            "even the first fhour failed"
        )

    manifest = {
        "source": source.name,
        "grid_type": source.grid_type,
        "run_type": run_type,
        "cycle": cycle_iso,
        "fhours": sorted(valid_times.keys()),
        "valid_times": [valid_times[fh] for fh in sorted(valid_times.keys())],
    }
    manifest_blob = json.dumps(manifest, separators=(",", ":")).encode("utf-8")

    if dry_run:
        out_dir = Path(__file__).parent.parent / "ingest_output"
        out_dir.mkdir(exist_ok=True)
        if topo_blob is not None:
            (out_dir / f"currents_{source.name}_topology.json.gz").write_bytes(topo_blob)
        for fh, blob in snapshot_blobs.items():
            (out_dir / f"currents_{source.name}_{cycle_iso}_{run_type}{fh:03d}.json.gz").write_bytes(blob)
        (out_dir / f"currents_{source.name}_{cycle_iso}_{run_type}_manifest.json").write_bytes(manifest_blob)
        print(
            f"[{source.name}] dry-run {run_type}-cycle complete: "
            f"{len(snapshot_blobs)} fhours, manifest written",
            flush=True,
        )
        return manifest

    client.setex(  # type: ignore[union-attr]
        _manifest_redis_key(source.name, cycle_iso, run_type),
        CYCLE_TTL_SECONDS,
        manifest_blob,
    )

    # Cycles index — shared across run types. Adding the same cycle twice
    # (once from the forecast worker, once from nowcast) is a no-op via
    # ZADD's existing-member behaviour.
    cycle_dt = datetime.strptime(cycle_iso, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    cycles_key = f"currents:{source.name}:cycles"
    client.zadd(cycles_key, {cycle_iso: cycle_dt.timestamp()})  # type: ignore[union-attr]
    # Keep the most recent 4 cycles per source. NetCDF blobs are large
    # enough that we trim more aggressively than the wind worker.
    client.zremrangebyrank(cycles_key, 0, -5)  # type: ignore[union-attr]

    print(
        f"[{source.name}] {run_type}-cycle ingest complete: "
        f"{len(valid_times)} fhours, cycle={cycle_iso}",
        flush=True,
    )
    return manifest


# ---------------------------------------------------------------------------
# Helpers


def _resolve(source_name: str) -> CurrentSource:
    if source_name not in CURRENT_SOURCES:
        raise ValueError(
            f"unknown current source: {source_name}. "
            f"valid: {sorted(CURRENT_SOURCES)}"
        )
    return CURRENT_SOURCES[source_name]


def _normalise_run_type(value: str) -> RunType:
    """Accept either short ('f','n') or long ('forecast','nowcast') forms."""
    key = value.lower()
    if key not in _RUN_TYPE_ALIASES:
        raise ValueError(
            f"unknown run type: {value!r}. valid: forecast, nowcast, f, n"
        )
    return _RUN_TYPE_ALIASES[key]  # type: ignore[return-value]


def main() -> None:
    parser = argparse.ArgumentParser(description="NOAA OFS currents ingestion worker")
    parser.add_argument("source", choices=sorted(CURRENT_SOURCES.keys()))
    parser.add_argument(
        "--run-type", default="forecast",
        help="forecast (default) | nowcast | f | n",
    )
    parser.add_argument(
        "--fhour", type=int,
        help="Single-fhour mode (default: full cycle ingest).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Write JSON to ./ingest_output/ instead of Redis/GCS.",
    )
    args = parser.parse_args()
    run_type = _normalise_run_type(args.run_type)

    if args.fhour is not None:
        ingest(args.source, fhour=args.fhour, run_type=run_type, dry_run=args.dry_run)
    else:
        ingest_cycle(args.source, run_type=run_type, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
