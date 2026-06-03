"""NOAA OFS currents ingestion -> native-grid u/v cache, per source + run type.

Production: runs as a Cloud Run Job per (source, run_type) pair — one for
each forecast cycle, one for each nowcast refresh. Scheduler triggers:

    forecast:  every 6h   (matches OFS cycle cadence)
    nowcast:   hourly     (refreshes the recent-past analyzed window)

Differs from ``weather_ingest`` in two important ways:

1. **No bbox clipping or regridding.** OFS data is preserved on its
   native grid (FVCOM unstructured mesh, or ROMS/POM curvilinear
   structured) so shoreline fidelity around complex embayments is not
   lost. ``services.currents.netcdf_extract`` does the format parsing.

2. **Static topology cached separately.** The mesh (FVCOM) or grid
   (ROMS) is identical across every cycle for a given source. It is
   written once under ``currents:{source}:topology`` and reused; the
   per-fhour blobs carry only u, v, valid_time.

Run types:

    forecast (``f``)  — files cover f000..f{forecast_horizon} forward
                        from cycle start.
    nowcast  (``n``)  — files cover n001..n{nowcast_horizon} BACKWARD
                        from cycle start (analyzed conditions in the
                        recent past).

After Phase 4 of the architecture review, the per-cycle orchestration
(fhour walk → Redis → GCS → manifest → cycles index) is shared with
``weather_ingest`` via
:class:`~app.services.ingest.cycle_pipeline.CyclicalIngestPipeline`.
Currents-specific concerns (NetCDF download, FVCOM/ROMS parsing,
topology write-once semantics) live in the
:class:`CurrentsSnapshotSource` adapter below.

Usage (from backend/):

    python -m workers.currents_ingest lmhofs --dry-run
    python -m workers.currents_ingest lmhofs --run-type nowcast --dry-run
    python -m workers.currents_ingest cbofs --fhour 1 --dry-run
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
from dataclasses import dataclass
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
from app.services.ingest import (
    CyclicalIngestPipeline,
    GcsArchive,
    SnapshotResult,
)
from app.services.ingest.archive import Archive
from app.services.redis_keys import (
    currents_cycles_index_key,
    currents_manifest_key,
    currents_snapshot_key,
    currents_topology_key,
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

# Keep the most recent 4 cycles per source. NetCDF blobs are large
# enough that we trim more aggressively than the wind worker.
CYCLES_TRIM_KEEP = 4

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
    """Download a NetCDF file to ``out``. Returns bytes received."""
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
    """Encode the static mesh / grid as gzipped JSON."""
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
    """Convert numpy array to nested Python lists, replacing NaN/Inf with None."""
    masked = np.where(np.isfinite(arr), arr, None)  # type: ignore[arg-type]
    return masked.tolist()


# ---------------------------------------------------------------------------
# Fetch one fhour — used by both single-fhour ``ingest()`` and the pipeline


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


# ---------------------------------------------------------------------------
# SnapshotSource adapter


@dataclass
class CurrentsSnapshotSource:
    """Adapter that plugs a (source, run_type) pair into the cycle pipeline.

    Holds the source config + run-type. Implements the
    :class:`~app.services.ingest.SnapshotSource` Protocol so the
    pipeline can drive a full-cycle ingest without knowing about
    FVCOM/ROMS NetCDF formats.

    Topology handling: the static mesh/grid is extracted from every
    fhour's NetCDF (it's part of the file) but we only write it to
    Redis/GCS once per worker invocation, gated by
    :meth:`persist_first_snapshot_extras`. The extracted topology
    is threaded from :meth:`fetch_snapshot` via ``SnapshotResult.extras``.
    """
    source: CurrentSource
    run_type: RunType

    @property
    def name(self) -> str:
        return f"{self.source.name}/{self.run_type}"

    @property
    def cycle_ttl_seconds(self) -> int:
        return CYCLE_TTL_SECONDS

    @property
    def cycles_trim_keep(self) -> int:
        return CYCLES_TRIM_KEEP

    def fhour_range(self) -> list[int]:
        return self.source.fhour_range(self.run_type)

    def fetch_snapshot(self, fhour: int) -> SnapshotResult:
        topology, snapshot, cycle_iso = _fetch_one(
            self.source, self.run_type, fhour,
        )
        snap_blob = _serialize_snapshot(snapshot, self.run_type)
        return SnapshotResult(
            blob_gz=snap_blob,
            cycle_iso=cycle_iso,
            valid_time_iso=snapshot.valid_time.isoformat(),  # type: ignore[union-attr]
            # Thread topology through to persist_first_snapshot_extras so
            # we don't re-extract it on the second fhour.
            extras={"topology": topology},
        )

    def snapshot_redis_key(self, cycle_iso: str, fhour: int) -> str:
        return currents_snapshot_key(self.source.name, cycle_iso, self.run_type, fhour)

    def manifest_redis_key(self, cycle_iso: str) -> str:
        return currents_manifest_key(self.source.name, cycle_iso, self.run_type)

    def cycles_index_redis_key(self) -> str:
        return currents_cycles_index_key(self.source.name)

    def snapshot_archive_path(self, cycle_iso: str, fhour: int) -> str:
        return f"{self.source.name}/{cycle_iso}/{self.run_type}{fhour:03d}.json.gz"

    def aliases_for_fhour(self, cycle_iso: str, fhour: int) -> list[str]:
        """No per-fhour aliases for currents."""
        return []

    def persist_first_snapshot_extras(
        self, *, redis_client, archive: Optional[Archive], snapshot: SnapshotResult,
    ) -> None:
        """Write the topology blob once per worker invocation, if not
        already present in Redis.

        The static topology is identical across every cycle of an OFS
        source. We write it on the FIRST fhour of the run rather than
        the FIRST cycle ever so that a Redis flush (which expires the
        topology TTL) self-heals on the next ingest.
        """
        if redis_client.exists(currents_topology_key(self.source.name)):
            return
        topology = snapshot.extras["topology"] if snapshot.extras else None
        if topology is None:
            return
        topo_blob = _serialize_topology(topology)
        redis_client.setex(
            currents_topology_key(self.source.name),
            TOPOLOGY_TTL_SECONDS,
            topo_blob,
        )
        if archive is not None:
            archive.upload(
                f"{self.source.name}/topology.json.gz",
                topo_blob,
                content_type="application/json",
                gzip_encoded=True,
            )
        print(f"  topology written ({len(topo_blob) / 1e3:.1f} KB gz)", flush=True)

    def build_manifest_fields(
        self, *, cycle_iso: str, fhours: list[int], valid_times: list[str],
    ) -> dict:
        return {
            "source": self.source.name,
            "grid_type": self.source.grid_type,
            "run_type": self.run_type,
            "cycle": cycle_iso,
            "fhours": fhours,
            "valid_times": valid_times,
        }

    def dry_run_snapshot_filename(self, cycle_iso: str, fhour: int) -> str:
        return f"currents_{self.source.name}_{cycle_iso}_{self.run_type}{fhour:03d}.json.gz"

    def dry_run_manifest_filename(self, cycle_iso: str) -> str:
        return f"currents_{self.source.name}_{cycle_iso}_{self.run_type}_manifest.json"


# ---------------------------------------------------------------------------
# Resolve / Redis / archive plumbing


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


def _redis_client() -> redis.Redis:
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=0,
    )


def _make_archive() -> GcsArchive:
    """Wire a GcsArchive against the currents bucket. Tests patch
    ``workers.currents_ingest.storage.Client`` so the existing
    storage-mock chain still works."""
    return GcsArchive.from_env("GCS_CURRENTS_BUCKET", storage_module=storage)


# ---------------------------------------------------------------------------
# Public entrypoints


def ingest(
    source_name: str,
    fhour: int,
    *,
    run_type: RunType = "f",
    dry_run: bool = False,
) -> dict:
    """Single-fhour ingest. Useful for ad-hoc backfills and testing.

    Preserves the existing API shape — the test suite probes it
    directly. The cycle-pipeline path is :func:`ingest_cycle`.
    """
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
    archive = _make_archive()
    if not client.exists(currents_topology_key(source.name)):
        client.setex(currents_topology_key(source.name), TOPOLOGY_TTL_SECONDS, topo_blob)
        archive.upload(
            f"{source.name}/topology.json.gz",
            topo_blob,
            content_type="application/json",
            gzip_encoded=True,
        )
        print(f"[{source.name}] topology written", flush=True)
    client.setex(
        currents_snapshot_key(source.name, cycle_iso, run_type, fhour),
        CYCLE_TTL_SECONDS,
        snap_blob,
    )
    archive.upload(
        f"{source.name}/{cycle_iso}/{run_type}{fhour:03d}.json.gz",
        snap_blob,
        content_type="application/json",
        gzip_encoded=True,
    )
    return {"source": source.name, "cycle": cycle_iso, "run_type": run_type, "fhour": fhour}


def ingest_cycle(
    source_name: str,
    *,
    run_type: RunType = "f",
    dry_run: bool = False,
) -> dict:
    """Ingest the full fhour sequence for the latest cycle of one run type.

    Delegates to :class:`CyclicalIngestPipeline`. Forecast and nowcast
    are independent run types of the same source — the worker is
    invoked once per (source, run_type) pair via separate Cloud
    Scheduler jobs, so this function knows about only one at a time.

    Returns the manifest dict so existing tests keep working.
    """
    source = _resolve(source_name)
    currents_source = CurrentsSnapshotSource(source=source, run_type=run_type)

    if dry_run:
        dry_run_dir = Path(__file__).parent.parent / "ingest_output"
        dry_run_dir.mkdir(exist_ok=True)
        pipeline = CyclicalIngestPipeline(
            currents_source,
            dry_run=True,
            dry_run_dir=dry_run_dir,
        )
    else:
        pipeline = CyclicalIngestPipeline(
            currents_source,
            redis_client=_redis_client(),
            archive=_make_archive(),
        )

    manifest = pipeline.run_cycle()
    return manifest.fields


# ---------------------------------------------------------------------------
# CLI


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
