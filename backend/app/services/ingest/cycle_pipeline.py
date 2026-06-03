# backend/app/services/ingest/cycle_pipeline.py
"""Shared orchestration for cycle-based ingest workers.

Weather (HRRR/GFS) and currents (NOAA OFS) — and every future cycle-
based worker (WaveWatch III, ECMWF wind, SST, …) — share this dance:

    1. Walk the source's fhour range.
    2. Fetch each fhour. Stop on the first 404 (cycle not fully published
       yet — keep what we have).
    3. Per fhour:
        a. Persist the snapshot blob to Redis under the per-fhour key.
        b. Archive the same blob to GCS at the per-fhour object path.
    4. On the FIRST persisted fhour, give the source a chance to write
       cycle-scoped one-time state (weather's ``:latest`` backwards-
       compat alias, currents' topology blob).
    5. After the loop, persist the manifest (Redis) and update the
       cycles sorted set (ZADD + ZREMRANGEBYRANK to trim).
    6. Dry-run: skip Redis + GCS, write each blob (and the manifest)
       to a local directory under the source's chosen filenames.

The source-specific concerns — URL building, parsing GRIB/NetCDF,
constructing the per-fhour blob, the manifest field set — live in a
:class:`SnapshotSource` adapter. The pipeline calls into the source
for: fhour range, single-fhour fetch, key/path builders, optional
hooks. Each source is ~100 LOC; the pipeline is the same ~150 LOC for
every cycle-based worker.

What this module does NOT own
-----------------------------
* Auth / job triggering — workers stay as Cloud Run Job entrypoints
  with their own argparse and Cloud Scheduler config.
* Source registry — the worker module owns its ``SOURCES`` dict; the
  pipeline only knows about the single configured source it was
  handed.
* Manifest schema — pinned by the source's
  :meth:`SnapshotSource.build_manifest_extras` method, not by this
  module. A new field on the wire is one source's concern, not a
  cross-source refactor.

Why a Protocol, not inheritance
-------------------------------
A source can be an ordinary class with no base — composition with the
pipeline is by structural typing. Tests use a small stub class
satisfying the Protocol; production sources (``WeatherSnapshotSource``,
``CurrentsSnapshotSource``) implement the same interface.
"""
from __future__ import annotations

import json
import logging
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from app.services.ingest.archive import Archive

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value types


@dataclass(frozen=True)
class SnapshotResult:
    """Per-fhour fetch result handed from source to pipeline.

    ``blob_gz`` is what gets persisted (Redis + archive). ``cycle_iso``
    and ``valid_time_iso`` flow into the manifest. ``extras`` is an
    opaque slot for source-specific state the source wants to remember
    between :meth:`SnapshotSource.fetch_snapshot` and
    :meth:`SnapshotSource.persist_first_snapshot_extras` — currents
    uses it to thread the parsed topology object through; weather
    leaves it ``None``.
    """
    blob_gz: bytes
    cycle_iso: str
    valid_time_iso: str
    extras: Any = None


@dataclass
class CycleManifest:
    """Manifest written at the end of a cycle ingest.

    ``fields`` carries the JSON-serialisable representation that the
    consumer (forecast/currents loader) will read. ``raw_blob`` is the
    encoded bytes the pipeline persists.
    """
    fields: dict
    raw_blob: bytes


# ---------------------------------------------------------------------------
# Source Protocol


@runtime_checkable
class SnapshotSource(Protocol):
    """Per-source adapter for the cycle pipeline.

    Implementations carry source-specific config (URLs, parsers, key
    builders) and are constructed once per worker invocation. The
    pipeline holds an instance and does not introspect it beyond the
    Protocol methods.
    """

    # Identity + behaviour
    @property
    def name(self) -> str: ...
    @property
    def cycle_ttl_seconds(self) -> int: ...
    @property
    def cycles_trim_keep(self) -> int:
        """How many recent cycles to retain in the sorted set."""
        ...

    # Discovery
    def fhour_range(self) -> list[int]: ...

    # Fetch
    def fetch_snapshot(self, fhour: int) -> SnapshotResult:
        """Download + parse + serialize one fhour.

        Should raise ``urllib.error.HTTPError`` with ``code == 404``
        when the fhour isn't published yet — the pipeline treats that
        as 'cycle not complete' and exits the loop gracefully.
        """
        ...

    # Persistence keys / paths
    def snapshot_redis_key(self, cycle_iso: str, fhour: int) -> str: ...
    def manifest_redis_key(self, cycle_iso: str) -> str: ...
    def cycles_index_redis_key(self) -> str: ...
    def snapshot_archive_path(self, cycle_iso: str, fhour: int) -> str: ...

    # Hooks
    def aliases_for_fhour(self, cycle_iso: str, fhour: int) -> list[str]:
        """Extra Redis alias keys to mirror the snapshot blob to.

        Default: ``[]``. Weather overrides to return its ``:latest``
        backwards-compat pointer when ``fhour == default_fhour``.
        Each alias key receives the same TTL as the canonical
        snapshot key. Runs only in non-dry-run mode.
        """
        ...

    def persist_first_snapshot_extras(
        self,
        *,
        redis_client,
        archive: Optional[Archive],
        snapshot: SnapshotResult,
    ) -> None:
        """Called once per cycle, after the first fhour is persisted.

        Default: no-op. Currents overrides to write the topology blob
        (Redis + archive) if not already present. Runs only in
        non-dry-run mode.
        """
        ...

    def build_manifest_fields(
        self,
        *,
        cycle_iso: str,
        fhours: list[int],
        valid_times: list[str],
    ) -> dict:
        """Compose the JSON manifest fields.

        Common fields (source name, cycle, fhours, valid_times) are
        the source's responsibility because manifest field naming is
        already part of the on-wire contract that consumers parse —
        keeping the source in charge means adding a manifest field is
        a one-file change.
        """
        ...

    # Dry-run filenames
    def dry_run_snapshot_filename(self, cycle_iso: str, fhour: int) -> str: ...
    def dry_run_manifest_filename(self, cycle_iso: str) -> str: ...


# ---------------------------------------------------------------------------
# The pipeline


class CyclicalIngestPipeline:
    """Runs one cycle's worth of ingest end-to-end for a given source.

    Worker entry points construct the pipeline with a source + Redis
    client + optional archive and call :meth:`run_cycle`. The same
    pipeline class drives weather, currents, and any future cycle-
    based feed — divergence stays inside each :class:`SnapshotSource`
    implementation.
    """

    def __init__(
        self,
        source: SnapshotSource,
        *,
        redis_client=None,
        archive: Optional[Archive] = None,
        dry_run: bool = False,
        dry_run_dir: Optional[Path] = None,
    ) -> None:
        if not dry_run and redis_client is None:
            raise ValueError(
                "redis_client is required for non-dry-run pipeline runs"
            )
        if dry_run and dry_run_dir is None:
            raise ValueError(
                "dry_run_dir is required when dry_run=True"
            )
        self.source = source
        self.redis = redis_client
        self.archive = archive
        self.dry_run = dry_run
        self.dry_run_dir = dry_run_dir

    # ── Persistence helpers (split out so tests can hook in) ──────────────

    def _persist_snapshot(
        self,
        cycle_iso: str,
        fhour: int,
        result: SnapshotResult,
    ) -> None:
        if self.dry_run:
            path = self.dry_run_dir / self.source.dry_run_snapshot_filename(  # type: ignore[operator]
                cycle_iso, fhour,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result.blob_gz)
            return

        key = self.source.snapshot_redis_key(cycle_iso, fhour)
        ttl = self.source.cycle_ttl_seconds
        self.redis.setex(key, ttl, result.blob_gz)

        for alias in self.source.aliases_for_fhour(cycle_iso, fhour):
            self.redis.setex(alias, ttl, result.blob_gz)

        if self.archive is not None:
            archive_path = self.source.snapshot_archive_path(cycle_iso, fhour)
            self.archive.upload(
                archive_path,
                result.blob_gz,
                content_type="application/json",
                gzip_encoded=True,
            )

    def _persist_manifest(self, cycle_iso: str, manifest: CycleManifest) -> None:
        if self.dry_run:
            path = self.dry_run_dir / self.source.dry_run_manifest_filename(  # type: ignore[operator]
                cycle_iso,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(manifest.raw_blob)
            return

        key = self.source.manifest_redis_key(cycle_iso)
        self.redis.setex(key, self.source.cycle_ttl_seconds, manifest.raw_blob)

    def _update_cycles_index(self, cycle_iso: str) -> None:
        if self.dry_run:
            return
        cycle_dt = datetime.strptime(cycle_iso, "%Y%m%dT%H%MZ").replace(
            tzinfo=timezone.utc,
        )
        cycles_key = self.source.cycles_index_redis_key()
        self.redis.zadd(cycles_key, {cycle_iso: cycle_dt.timestamp()})
        # Trim to the N most recent cycles. ZREMRANGEBYRANK 0 -(keep+1)
        # removes everything except the top `keep` by score.
        self.redis.zremrangebyrank(cycles_key, 0, -self.source.cycles_trim_keep - 1)

    # ── Main entry point ──────────────────────────────────────────────────

    def run_cycle(self) -> CycleManifest:
        """Ingest the full forecast sequence for the source's latest cycle.

        Returns the :class:`CycleManifest` (fields + raw blob). Raises
        :class:`RuntimeError` if the first fhour fails — that means
        the cycle isn't published at all, which is a different signal
        from "partially published" (the latter just stops the loop
        and keeps what arrived).
        """
        fhours = self.source.fhour_range()
        log.info(
            "[%s] cycle ingest, fhours=%s..%s (%s files), dry_run=%s",
            self.source.name, fhours[0], fhours[-1], len(fhours), self.dry_run,
        )

        cycle_iso: Optional[str] = None
        valid_times: dict[int, str] = {}
        first_persisted = False

        for fh in fhours:
            try:
                result = self.source.fetch_snapshot(fh)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    log.info(
                        "[%s] fhour %s: 404 (not yet published) — stopping cycle",
                        self.source.name, fh,
                    )
                    break
                raise

            cycle_iso = result.cycle_iso
            valid_times[fh] = result.valid_time_iso

            self._persist_snapshot(cycle_iso, fh, result)

            if not first_persisted:
                if not self.dry_run:
                    self.source.persist_first_snapshot_extras(
                        redis_client=self.redis,
                        archive=self.archive,
                        snapshot=result,
                    )
                first_persisted = True

        if cycle_iso is None:
            raise RuntimeError(
                f"[{self.source.name}] no fhours ingested — even the first failed"
            )

        ordered_fhours = sorted(valid_times)
        manifest_fields = self.source.build_manifest_fields(
            cycle_iso=cycle_iso,
            fhours=ordered_fhours,
            valid_times=[valid_times[fh] for fh in ordered_fhours],
        )
        manifest_blob = json.dumps(manifest_fields, separators=(",", ":")).encode("utf-8")
        manifest = CycleManifest(fields=manifest_fields, raw_blob=manifest_blob)

        self._persist_manifest(cycle_iso, manifest)
        self._update_cycles_index(cycle_iso)

        log.info(
            "[%s] cycle ingest complete: %s fhours, cycle=%s",
            self.source.name, len(valid_times), cycle_iso,
        )
        return manifest
