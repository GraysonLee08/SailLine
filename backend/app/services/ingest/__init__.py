# backend/app/services/ingest/__init__.py
"""Shared building blocks for the four NOAA ingest workers.

Two abstractions, chosen for the right level of reuse:

* :class:`~app.services.ingest.archive.GcsArchive` — small wrapper
  over the GCS bucket / object upload pattern that every ingest
  worker uses. Lets the cycle pipeline compose with archive uploads
  WITHOUT forcing one-shot workers (bathymetry, ENC) into a cycle
  shape they don't have.

* :class:`~app.services.ingest.cycle_pipeline.CyclicalIngestPipeline`
  — the fetch → serialize → Redis → GCS → manifest → cycles-index
  dance shared by weather and currents (and any future cycle-based
  ingest worker: WaveWatch III, ECMWF wind, SST, …). Per-source
  details live in a :class:`SnapshotSource` adapter.

Bathymetry and ENC stay on their own paths — they're one-shot static
datasets, not cycle-based feeds. They use ``GcsArchive`` directly.
"""
from app.services.ingest.archive import GcsArchive
from app.services.ingest.cycle_pipeline import (
    CyclicalIngestPipeline,
    CycleManifest,
    SnapshotResult,
    SnapshotSource,
)

__all__ = [
    "CyclicalIngestPipeline",
    "CycleManifest",
    "GcsArchive",
    "SnapshotResult",
    "SnapshotSource",
]
