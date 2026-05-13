"""Surface currents service — NOAA OFS NetCDF ingest, native-grid storage,
and a time-bracketing sampler that plugs into the routing engine.

Exposed surface:

* ``FvcomMesh`` / ``FvcomSnapshot`` — unstructured triangular mesh + per-fhour
  velocity arrays for the Great Lakes + SFBOFS.
* ``RomsGrid`` / ``RomsSnapshot`` — curvilinear structured grid + per-fhour
  velocity arrays for ROMS / POM coastal sources.
* ``FvcomCurrentField`` / ``RomsCurrentField`` — single-snapshot samplers
  with a uniform ``.sample(lat, lon, valid_time) -> (uc_ms, vc_ms) | None``
  interface so the routing engine can consume either grid family.
* ``CurrentForecast`` — time-bracketing wrapper over an ordered list of
  field snapshots; mirrors the role of ``WindForecast``.
* ``CurrentsUnavailable`` — raised when no OFS source covers the race
  (handled gracefully by the router; routes still compute, just without
  the current-vector addition).

Storage layout in Redis (per source, no region — the source name already
identifies the water body):

    currents:{source}:mesh                       gzipped JSON, static per source
    currents:{source}:{cycle}:f{fhour:03d}       gzipped JSON, per-fhour u/v
    currents:{source}:{cycle}:manifest           cycle metadata
    currents:{source}:cycles                     sorted set, score = cycle epoch
"""
from __future__ import annotations

from .fields import (
    CurrentField,
    CurrentForecast,
    CurrentsUnavailable,
    FvcomCurrentField,
    RomsCurrentField,
    field_for_snapshot,
)
from .loader import (
    DEFAULT_RACE_DURATION_HOURS,
    load_currents_for_race,
)
from .netcdf_extract import (
    FvcomMesh,
    FvcomSnapshot,
    RomsGrid,
    RomsSnapshot,
    extract_fvcom,
    extract_roms,
)

__all__ = [
    "CurrentField",
    "CurrentForecast",
    "CurrentsUnavailable",
    "DEFAULT_RACE_DURATION_HOURS",
    "FvcomCurrentField",
    "FvcomMesh",
    "FvcomSnapshot",
    "RomsCurrentField",
    "RomsGrid",
    "RomsSnapshot",
    "extract_fvcom",
    "extract_roms",
    "field_for_snapshot",
    "load_currents_for_race",
]
