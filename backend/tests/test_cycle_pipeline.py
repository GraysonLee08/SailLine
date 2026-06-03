# backend/tests/test_cycle_pipeline.py
"""Tests for ``app.services.ingest.cycle_pipeline.CyclicalIngestPipeline``.

The pipeline is the shared engine room for cycle-based ingest workers
(weather, currents, and any future cycle-based feed). These tests use
a small stub :class:`SnapshotSource` to verify the orchestration:

* Walks the fhour range, calling fetch_snapshot for each.
* Stops cleanly on a 404 (cycle not fully published yet).
* Raises if the FIRST fhour fails (cycle isn't published at all).
* Persists snapshots to Redis at the source's key.
* Mirrors snapshots to any aliases the source declares per-fhour.
* Uploads to the GCS archive at the source's path, with gzip encoding.
* Persists the manifest at the source's manifest key.
* Updates the cycles sorted set + trims to the source's keep count.
* Calls persist_first_snapshot_extras once, after the first fhour.
* Dry-run writes blobs + manifest to disk under the source's filenames,
  with no Redis or archive touches.
"""
from __future__ import annotations

import gzip
import json
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from app.services.ingest.archive import Archive
from app.services.ingest.cycle_pipeline import (
    CyclicalIngestPipeline,
    SnapshotResult,
)


# ─── Stub source ────────────────────────────────────────────────────────


@dataclass
class _StubSource:
    """Minimal SnapshotSource implementation for pipeline tests."""
    fhours: list[int]
    fetched: list[int] = field(default_factory=list)
    first_snapshot_extras_call_count: int = 0
    raise_404_at: Optional[int] = None
    raise_500_at: Optional[int] = None
    aliases: dict[int, list[str]] = field(default_factory=dict)
    cycle_iso: str = "20260603T1200Z"

    name: str = "stub"
    cycle_ttl_seconds: int = 3600
    cycles_trim_keep: int = 8

    def fhour_range(self) -> list[int]:
        return self.fhours

    def fetch_snapshot(self, fhour: int) -> SnapshotResult:
        self.fetched.append(fhour)
        if fhour == self.raise_404_at:
            raise urllib.error.HTTPError(
                "url", 404, "Not Found", hdrs=None, fp=None,  # type: ignore[arg-type]
            )
        if fhour == self.raise_500_at:
            raise urllib.error.HTTPError(
                "url", 500, "Server Error", hdrs=None, fp=None,  # type: ignore[arg-type]
            )
        return SnapshotResult(
            blob_gz=f"blob-fh{fhour}".encode(),
            cycle_iso=self.cycle_iso,
            valid_time_iso=f"2026-06-03T{12 + fhour:02d}:00:00+00:00",
            extras={"fhour": fhour},
        )

    def snapshot_redis_key(self, cycle_iso: str, fhour: int) -> str:
        return f"stub:{cycle_iso}:f{fhour:03d}"

    def manifest_redis_key(self, cycle_iso: str) -> str:
        return f"stub:{cycle_iso}:manifest"

    def cycles_index_redis_key(self) -> str:
        return "stub:cycles"

    def snapshot_archive_path(self, cycle_iso: str, fhour: int) -> str:
        return f"stub/{cycle_iso}/f{fhour:03d}.json.gz"

    def aliases_for_fhour(self, cycle_iso: str, fhour: int) -> list[str]:
        return self.aliases.get(fhour, [])

    def persist_first_snapshot_extras(
        self, *, redis_client, archive: Optional[Archive], snapshot: SnapshotResult,
    ) -> None:
        self.first_snapshot_extras_call_count += 1

    def build_manifest_fields(
        self, *, cycle_iso: str, fhours: list[int], valid_times: list[str],
    ) -> dict:
        return {
            "source": self.name,
            "cycle": cycle_iso,
            "fhours": fhours,
            "valid_times": valid_times,
        }

    def dry_run_snapshot_filename(self, cycle_iso: str, fhour: int) -> str:
        return f"{self.name}_{cycle_iso}_f{fhour:03d}.json.gz"

    def dry_run_manifest_filename(self, cycle_iso: str) -> str:
        return f"{self.name}_{cycle_iso}_manifest.json"


# ─── Production-run path ────────────────────────────────────────────────


def test_run_cycle_walks_full_fhour_range_and_writes_each():
    src = _StubSource(fhours=[0, 1, 2])
    redis_client = MagicMock()
    archive = MagicMock(spec=Archive)

    pipeline = CyclicalIngestPipeline(
        src, redis_client=redis_client, archive=archive,
    )
    manifest = pipeline.run_cycle()

    assert src.fetched == [0, 1, 2]
    # Each fhour written to Redis + archive once.
    setex_keys = [c.args[0] for c in redis_client.setex.call_args_list]
    for fh in (0, 1, 2):
        assert f"stub:20260603T1200Z:f{fh:03d}" in setex_keys
    archive_calls = [c.args[0] for c in archive.upload.call_args_list]
    for fh in (0, 1, 2):
        assert f"stub/20260603T1200Z/f{fh:03d}.json.gz" in archive_calls
    # Manifest fields match what build_manifest_fields returned.
    assert manifest.fields["fhours"] == [0, 1, 2]
    assert manifest.fields["cycle"] == "20260603T1200Z"


def test_run_cycle_persists_manifest_at_source_key():
    src = _StubSource(fhours=[0, 1])
    redis_client = MagicMock()
    pipeline = CyclicalIngestPipeline(src, redis_client=redis_client, archive=None)
    pipeline.run_cycle()

    setex_keys = [c.args[0] for c in redis_client.setex.call_args_list]
    assert "stub:20260603T1200Z:manifest" in setex_keys


def test_run_cycle_updates_cycles_index_and_trims():
    src = _StubSource(fhours=[0, 1], cycles_trim_keep=4)
    redis_client = MagicMock()
    pipeline = CyclicalIngestPipeline(src, redis_client=redis_client, archive=None)
    pipeline.run_cycle()

    redis_client.zadd.assert_called_once()
    zadd_args = redis_client.zadd.call_args.args
    assert zadd_args[0] == "stub:cycles"
    assert "20260603T1200Z" in zadd_args[1]
    redis_client.zremrangebyrank.assert_called_once_with(
        "stub:cycles", 0, -5,
    )


def test_run_cycle_stops_on_404_after_partial_success():
    """404 mid-cycle: keep the fhours we have, write the manifest with
    just those, do not raise. Cycle isn't fully published yet — the
    next ingest pass will fill in the gap."""
    src = _StubSource(fhours=[0, 1, 2, 3], raise_404_at=2)
    redis_client = MagicMock()
    pipeline = CyclicalIngestPipeline(src, redis_client=redis_client, archive=None)
    manifest = pipeline.run_cycle()

    # fhour 2 raised; loop stopped before fhour 3.
    assert src.fetched == [0, 1, 2]
    assert manifest.fields["fhours"] == [0, 1]


def test_run_cycle_raises_on_404_for_first_fhour():
    """No fhours ingested at all — cycle isn't published. Raise so the
    Cloud Run Job exits non-zero and Scheduler retries on the next
    cadence."""
    src = _StubSource(fhours=[0, 1], raise_404_at=0)
    redis_client = MagicMock()
    pipeline = CyclicalIngestPipeline(src, redis_client=redis_client, archive=None)
    with pytest.raises(RuntimeError, match="no fhours ingested"):
        pipeline.run_cycle()


def test_run_cycle_propagates_5xx_errors():
    """5xx must not be silently treated as 'cycle not published'. The
    fetch_snapshot's own retry policy gave up — we want loud failure."""
    src = _StubSource(fhours=[0, 1], raise_500_at=1)
    redis_client = MagicMock()
    pipeline = CyclicalIngestPipeline(src, redis_client=redis_client, archive=None)
    with pytest.raises(urllib.error.HTTPError):
        pipeline.run_cycle()


# ─── Hooks ──────────────────────────────────────────────────────────────


def test_first_snapshot_extras_called_once():
    """The hook fires exactly once per cycle, after the first fhour is
    persisted. Both currents (topology write) and any future once-per-
    cycle bookkeeping depend on this contract."""
    src = _StubSource(fhours=[0, 1, 2])
    redis_client = MagicMock()
    pipeline = CyclicalIngestPipeline(src, redis_client=redis_client, archive=None)
    pipeline.run_cycle()

    assert src.first_snapshot_extras_call_count == 1


def test_aliases_for_fhour_mirror_blob_to_extra_keys():
    """Weather's `:latest` alias pattern: source declares an extra Redis
    key for the fhour, pipeline writes the same blob to it with the same TTL."""
    src = _StubSource(
        fhours=[0, 1, 2],
        aliases={1: ["stub:latest"]},
    )
    redis_client = MagicMock()
    pipeline = CyclicalIngestPipeline(src, redis_client=redis_client, archive=None)
    pipeline.run_cycle()

    setex_calls = [(c.args[0], c.args[1], c.args[2]) for c in redis_client.setex.call_args_list]
    # fhour 1's canonical key AND the alias both get b'blob-fh1' at the source's TTL.
    assert ("stub:20260603T1200Z:f001", src.cycle_ttl_seconds, b"blob-fh1") in setex_calls
    assert ("stub:latest", src.cycle_ttl_seconds, b"blob-fh1") in setex_calls
    # Other fhours don't get the alias.
    assert ("stub:latest", src.cycle_ttl_seconds, b"blob-fh0") not in setex_calls


# ─── Archive composability ──────────────────────────────────────────────


def test_archive_none_skips_gcs_uploads():
    """Dev posture: pass archive=None, run pipeline against a real or
    fake Redis, skip GCS entirely. Lets developers test ingest end-to-
    end without GCP credentials."""
    src = _StubSource(fhours=[0, 1])
    redis_client = MagicMock()
    pipeline = CyclicalIngestPipeline(src, redis_client=redis_client, archive=None)
    pipeline.run_cycle()
    # Redis writes happened; no archive object was passed in to call.
    assert redis_client.setex.called


def test_archive_uploads_with_gzip_encoding():
    """Every snapshot upload must carry content_encoding=gzip. The
    Phase-4 commit centralised this so the four workers can't drift."""
    src = _StubSource(fhours=[0])
    redis_client = MagicMock()
    archive = MagicMock(spec=Archive)
    pipeline = CyclicalIngestPipeline(src, redis_client=redis_client, archive=archive)
    pipeline.run_cycle()

    archive.upload.assert_called_once()
    kwargs = archive.upload.call_args.kwargs
    assert kwargs["gzip_encoded"] is True
    assert kwargs["content_type"] == "application/json"


# ─── Dry-run posture ────────────────────────────────────────────────────


def test_dry_run_writes_blobs_to_disk_under_source_filenames(tmp_path):
    src = _StubSource(fhours=[0, 1])
    pipeline = CyclicalIngestPipeline(
        src, dry_run=True, dry_run_dir=tmp_path,
    )
    pipeline.run_cycle()

    assert (tmp_path / "stub_20260603T1200Z_f000.json.gz").exists()
    assert (tmp_path / "stub_20260603T1200Z_f001.json.gz").exists()
    assert (tmp_path / "stub_20260603T1200Z_manifest.json").exists()


def test_dry_run_skips_first_snapshot_extras():
    """The hook runs only in production mode — dry-run should never
    write to Redis, even via a source hook."""
    src = _StubSource(fhours=[0, 1])
    pipeline = CyclicalIngestPipeline(
        src, dry_run=True, dry_run_dir=Path("/tmp"),
    )
    # Avoid actually writing to /tmp by patching the persist methods.
    # We only care that the hook didn't fire.
    pipeline._persist_snapshot = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline._persist_manifest = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline.run_cycle()

    assert src.first_snapshot_extras_call_count == 0


def test_dry_run_manifest_round_trips_through_json(tmp_path):
    """Manifest written in dry-run mode must be valid JSON consumers
    can read back — the loader doesn't get a separate dry-run path."""
    src = _StubSource(fhours=[0, 1])
    pipeline = CyclicalIngestPipeline(
        src, dry_run=True, dry_run_dir=tmp_path,
    )
    pipeline.run_cycle()

    manifest_path = tmp_path / "stub_20260603T1200Z_manifest.json"
    data = json.loads(manifest_path.read_text())
    assert data["fhours"] == [0, 1]
    assert data["cycle"] == "20260603T1200Z"


# ─── Constructor guards ─────────────────────────────────────────────────


def test_non_dry_run_requires_redis_client():
    with pytest.raises(ValueError, match="redis_client is required"):
        CyclicalIngestPipeline(_StubSource(fhours=[0]))


def test_dry_run_requires_dry_run_dir():
    with pytest.raises(ValueError, match="dry_run_dir is required"):
        CyclicalIngestPipeline(_StubSource(fhours=[0]), dry_run=True)
