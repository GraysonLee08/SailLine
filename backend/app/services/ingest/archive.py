# backend/app/services/ingest/archive.py
"""Thin wrapper over the per-worker 'upload a blob to GCS' pattern.

Before this module existed, four workers (weather, currents,
bathymetry, ENC) each carried their own ``_write_gcs(...)`` or
``upload_to_gcs(...)`` helper plus their own ``GCS_*_BUCKET`` env
variable lookup. Drift risk: the gzip ``content_encoding`` header was
set in three of the four and forgotten in one.

What this module owns
---------------------
* Bucket-name lookup from an env variable (one place).
* The ``upload_from_string`` invocation with consistent ``content_type``
  + optional ``content_encoding=gzip`` handling.
* The ``gs://`` URI format for log lines.

What this module does NOT own
-----------------------------
* Path layout — every worker has its own per-domain prefix
  (``hrrr/conus/{cycle}/f{NNN}.json.gz``, ``bathymetry/{region}/depth.npz``,
  ``charts/{region}/hazards.geojson``). The caller passes the full
  path; this module only writes to it.
* Bucket selection — domain-scoped buckets (one per source family)
  are kept because the existing IAM is already wired against them.
  A future consolidation to one bucket with prefixes is a separate
  ticket; when it happens, ``from_env`` is the one site to change.

Composition with :class:`~app.services.ingest.cycle_pipeline.CyclicalIngestPipeline`
-----------------------------------------------------------------------------------
The cycle pipeline takes ``archive: GcsArchive | None``. ``None`` is
the dev-run posture (writes go to Redis / disk only, no GCS calls).
Tests inject a stub :class:`Archive` Protocol implementation.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable


log = logging.getLogger(__name__)


@runtime_checkable
class Archive(Protocol):
    """Minimal upload-only blob-store interface.

    The cycle pipeline and one-shot workers both speak this Protocol.
    Marked ``@runtime_checkable`` so tests asserting "the worker
    received an archive" don't need to introspect the concrete type.
    """
    def upload(
        self,
        path: str,
        blob: bytes,
        *,
        content_type: str = "application/json",
        gzip_encoded: bool = False,
    ) -> str: ...

    def exists(self, path: str) -> bool: ...


class GcsArchive:
    """GCS-backed implementation of :class:`Archive`.

    Constructed with a ``google.cloud.storage.Bucket`` instance — the
    bucket is injected rather than looked up internally so tests can
    pass a ``MagicMock`` bucket and assert against ``bucket.blob(...)``
    / ``blob.upload_from_string(...)`` exactly as they did against the
    pre-collapse workers' inline GCS calls.

    Use :func:`from_env` from worker entrypoints to wire up the real
    thing.
    """

    def __init__(self, bucket) -> None:
        # `bucket` is a `google.cloud.storage.Bucket` in prod; left
        # type-loose so test mocks (MagicMock) drop in without
        # google-cloud-storage stubs in the type checker.
        self._bucket = bucket

    @classmethod
    def from_env(cls, env_var: str, *, storage_module) -> "GcsArchive":
        """Build a :class:`GcsArchive` from ``os.environ[env_var]``.

        ``storage_module`` is passed in so worker modules can keep
        their ``from google.cloud import storage`` at module scope —
        the worker imports ``storage`` once and hands it here. This
        keeps the existing test patches against
        ``workers.<name>.storage.Client`` working without change.

        Raises:
            RuntimeError: if the env variable isn't set.
        """
        bucket_name = os.environ.get(env_var)
        if not bucket_name:
            raise RuntimeError(
                f"{env_var} env var not set; cannot upload to GCS"
            )
        client = storage_module.Client()
        return cls(client.bucket(bucket_name))

    @property
    def bucket_name(self) -> str:
        return self._bucket.name

    def upload(
        self,
        path: str,
        blob: bytes,
        *,
        content_type: str = "application/json",
        gzip_encoded: bool = False,
    ) -> str:
        """Upload ``blob`` to ``path`` in this bucket.

        ``gzip_encoded=True`` sets ``Content-Encoding: gzip`` on the
        object so GCS transparently decompresses for clients that
        advertise ``Accept-Encoding: gzip`` while preserving the
        compressed-at-rest storage. Every ingest worker stores its
        per-fhour blobs gzipped, so this is the common case.

        Returns the ``gs://`` URI for logging.
        """
        obj = self._bucket.blob(path)
        if gzip_encoded:
            obj.content_encoding = "gzip"
        obj.upload_from_string(blob, content_type=content_type)
        return f"gs://{self._bucket.name}/{path}"

    def exists(self, path: str) -> bool:
        return self._bucket.blob(path).exists()
