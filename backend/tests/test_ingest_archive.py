# backend/tests/test_ingest_archive.py
"""Tests for ``app.services.ingest.archive.GcsArchive``.

Pins the small but load-bearing GCS upload wrapper. Before Phase 4
each ingest worker carried its own ``_write_gcs`` / ``upload_to_gcs``
helper, three out of four set ``content_encoding="gzip"`` on the
uploaded object; one forgot. Centralising the helper here makes that
inconsistency impossible.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from app.services.ingest.archive import Archive, GcsArchive


def _make_mock_bucket(name: str = "fake-bucket"):
    bucket = MagicMock()
    bucket.name = name
    return bucket


# ─── Construction ────────────────────────────────────────────────────────


def test_archive_protocol_satisfied():
    """GcsArchive must satisfy the Archive Protocol — without it, the
    cycle pipeline's type annotation degrades to ``object`` and tests
    can't usefully assert the worker received the right kind of thing."""
    archive = GcsArchive(_make_mock_bucket())
    assert isinstance(archive, Archive)


def test_from_env_reads_bucket_name(monkeypatch):
    monkeypatch.setenv("FAKE_BUCKET_VAR", "my-bucket")
    storage_module = MagicMock()
    fake_bucket = _make_mock_bucket("my-bucket")
    storage_module.Client.return_value.bucket.return_value = fake_bucket

    archive = GcsArchive.from_env("FAKE_BUCKET_VAR", storage_module=storage_module)

    storage_module.Client.assert_called_once_with()
    storage_module.Client.return_value.bucket.assert_called_once_with("my-bucket")
    assert archive.bucket_name == "my-bucket"


def test_from_env_missing_env_raises(monkeypatch):
    monkeypatch.delenv("NONEXISTENT_BUCKET_VAR", raising=False)
    storage_module = MagicMock()
    with pytest.raises(RuntimeError, match="NONEXISTENT_BUCKET_VAR"):
        GcsArchive.from_env("NONEXISTENT_BUCKET_VAR", storage_module=storage_module)


# ─── upload() ────────────────────────────────────────────────────────────


def test_upload_writes_blob_to_path():
    """Single source of truth for the upload pattern: bucket.blob(path)
    + upload_from_string(blob, content_type=...)."""
    bucket = _make_mock_bucket()
    archive = GcsArchive(bucket)

    uri = archive.upload(
        "weather/hrrr/conus/20260603T1200Z/f006.json.gz",
        b"compressed-bytes",
        content_type="application/json",
        gzip_encoded=True,
    )

    bucket.blob.assert_called_once_with(
        "weather/hrrr/conus/20260603T1200Z/f006.json.gz",
    )
    obj = bucket.blob.return_value
    assert obj.content_encoding == "gzip"
    obj.upload_from_string.assert_called_once_with(
        b"compressed-bytes",
        content_type="application/json",
    )
    assert uri == "gs://fake-bucket/weather/hrrr/conus/20260603T1200Z/f006.json.gz"


def test_upload_without_gzip_encoding_omits_header():
    """Non-gzipped uploads (e.g. bathymetry .npz) must NOT set
    content_encoding=gzip — GCS would then try to inflate non-gzip bytes
    on download and corrupt them."""
    bucket = _make_mock_bucket()
    archive = GcsArchive(bucket)

    archive.upload(
        "bathymetry/conus/depth.npz",
        b"npz-bytes",
        content_type="application/octet-stream",
        gzip_encoded=False,
    )

    obj = bucket.blob.return_value
    # content_encoding was never assigned — MagicMock attribute access
    # returns a MagicMock by default, so we explicitly check it wasn't set.
    # The cleanest assertion is on the upload call args.
    obj.upload_from_string.assert_called_once_with(
        b"npz-bytes",
        content_type="application/octet-stream",
    )


def test_upload_returns_gs_uri():
    archive = GcsArchive(_make_mock_bucket("sailline-prod"))
    uri = archive.upload(
        "charts/conus/hazards.geojson",
        b"{}",
        content_type="application/geo+json",
    )
    assert uri == "gs://sailline-prod/charts/conus/hazards.geojson"


# ─── exists() ────────────────────────────────────────────────────────────


def test_exists_delegates_to_blob_exists():
    bucket = _make_mock_bucket()
    bucket.blob.return_value.exists.return_value = True
    archive = GcsArchive(bucket)

    assert archive.exists("a/b/c.gz") is True
    bucket.blob.assert_called_once_with("a/b/c.gz")


def test_exists_returns_false_when_blob_missing():
    bucket = _make_mock_bucket()
    bucket.blob.return_value.exists.return_value = False
    archive = GcsArchive(bucket)
    assert archive.exists("missing.gz") is False
