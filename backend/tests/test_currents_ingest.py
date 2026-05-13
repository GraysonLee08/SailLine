# backend/tests/test_currents_ingest.py
"""Tests for the currents ingest worker.

Avoids real NetCDF I/O — covers:
  - latest_cycle math (publish-lag aware)
  - _resolve unknown source guard
  - _normalise_run_type aliases
  - _serialize_topology / _serialize_snapshot round-trip via loader inverse
  - ingest_cycle in dry-run mode with _fetch_one stubbed
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest

from app.currents_regions import get as get_source
from app.services.currents.netcdf_extract import (
    FvcomMesh,
    FvcomSnapshot,
    RomsGrid,
    RomsSnapshot,
)
from workers import currents_ingest
from workers.currents_ingest import (
    _normalise_run_type,
    _resolve,
    _serialize_snapshot,
    _serialize_topology,
    latest_cycle,
)


# ─── latest_cycle ────────────────────────────────────────────────────────


def test_latest_cycle_respects_publish_lag(monkeypatch):
    """Just after a 12Z cycle, publish_lag should select 06Z (still safe)."""
    src = get_source("lmhofs")
    # Force 'now' to 14Z on 2026-05-13. publish_lag is 4h on default sources,
    # so effective 'now' is 10Z → cycle = floor(10/6)*6 = 06Z.
    fake_now = datetime(2026, 5, 13, 14, tzinfo=timezone.utc)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    monkeypatch.setattr("workers.currents_ingest.datetime", _DT)
    date, cycle = latest_cycle(src)
    assert date == "20260513"
    assert cycle == 6


# ─── _resolve / _normalise_run_type ─────────────────────────────────────


def test_resolve_unknown_source():
    with pytest.raises(ValueError, match="unknown current source"):
        _resolve("not_a_source")


def test_normalise_run_type_long_forms():
    assert _normalise_run_type("forecast") == "f"
    assert _normalise_run_type("nowcast") == "n"


def test_normalise_run_type_short_forms():
    assert _normalise_run_type("f") == "f"
    assert _normalise_run_type("n") == "n"


def test_normalise_run_type_case_insensitive():
    assert _normalise_run_type("FORECAST") == "f"


def test_normalise_run_type_unknown():
    with pytest.raises(ValueError):
        _normalise_run_type("hindcast")


# ─── Serialisation round-trip ───────────────────────────────────────────


def test_fvcom_topology_roundtrip():
    """Serialise an FVCOM mesh; inflate; verify structure."""
    mesh = FvcomMesh(
        source="testfv",
        lats=np.array([0.0, 1.0, 0.0, 1.0], dtype=np.float32),
        lons=np.array([0.0, 0.0, 1.0, 1.0], dtype=np.float32),
        triangles=np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int32),
    )
    blob = _serialize_topology(mesh)
    payload = json.loads(gzip.decompress(blob))
    assert payload["kind"] == "fvcom"
    assert payload["source"] == "testfv"
    assert payload["n_nodes"] == 4
    assert payload["n_triangles"] == 2
    assert payload["lats"] == [0.0, 1.0, 0.0, 1.0]
    assert payload["triangles"] == [[0, 1, 2], [1, 3, 2]]


def test_roms_topology_roundtrip():
    grid = RomsGrid(
        source="testroms",
        lats=np.array([[0.0, 1.0], [0.0, 1.0]], dtype=np.float32),
        lons=np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float32),
        mask=np.array([[True, True], [False, True]], dtype=bool),
        angle=np.zeros((2, 2), dtype=np.float32),
    )
    blob = _serialize_topology(grid)
    payload = json.loads(gzip.decompress(blob))
    assert payload["kind"] == "roms"
    assert payload["shape"] == [2, 2]
    # Mask should serialise as nested bools, not numpy
    assert payload["mask"] == [[True, True], [False, True]]


def test_fvcom_snapshot_serialises_run_type():
    """The run_type tag must be embedded so the loader can distinguish."""
    snap = FvcomSnapshot(
        source="testfv",
        cycle_iso="20260513T0000Z",
        reference_time=datetime(2026, 5, 13, tzinfo=timezone.utc),
        valid_time=datetime(2026, 5, 13, 3, tzinfo=timezone.utc),
        fhour=3,
        u=np.array([0.1, 0.2, np.nan, 0.4], dtype=np.float32),
        v=np.array([0.5, 0.6, 0.7, 0.8], dtype=np.float32),
    )
    blob = _serialize_snapshot(snap, "f")
    payload = json.loads(gzip.decompress(blob))
    assert payload["run_type"] == "f"
    assert payload["fhour"] == 3
    # NaN should serialise as None for JSON round-trip.
    assert payload["u"][2] is None


def test_roms_snapshot_carries_shape():
    grid_shape = (3, 4)
    snap = RomsSnapshot(
        source="testroms",
        cycle_iso="20260513T0000Z",
        reference_time=datetime(2026, 5, 13, tzinfo=timezone.utc),
        valid_time=datetime(2026, 5, 13, 1, tzinfo=timezone.utc),
        fhour=1,
        u=np.ones(grid_shape, dtype=np.float32),
        v=np.zeros(grid_shape, dtype=np.float32),
    )
    blob = _serialize_snapshot(snap, "n")
    payload = json.loads(gzip.decompress(blob))
    assert payload["shape"] == [3, 4]
    assert payload["run_type"] == "n"


# ─── ingest_cycle dry-run ───────────────────────────────────────────────


def test_ingest_cycle_dry_run_stops_on_404(monkeypatch, tmp_path):
    """A 404 on fhour > 0 stops the cycle and writes what was fetched."""
    import urllib.error

    src = get_source("lmhofs")
    mesh = FvcomMesh(
        source=src.name,
        lats=np.array([41.0, 42.0, 41.0, 42.0], dtype=np.float32),
        lons=np.array([-88.0, -88.0, -87.0, -87.0], dtype=np.float32),
        triangles=np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int32),
    )

    def fake_fetch_one(source, run_type, fhour):
        if fhour > 1:
            raise urllib.error.HTTPError(
                "url", 404, "Not Found", hdrs=None, fp=None,  # type: ignore[arg-type]
            )
        snap = FvcomSnapshot(
            source=source.name,
            cycle_iso="20260513T1200Z",
            reference_time=datetime(2026, 5, 13, 12, tzinfo=timezone.utc),
            valid_time=datetime(2026, 5, 13, 12 + fhour, tzinfo=timezone.utc),
            fhour=fhour,
            u=np.array([0.1] * 4, dtype=np.float32),
            v=np.array([0.0] * 4, dtype=np.float32),
        )
        return mesh, snap, snap.cycle_iso

    # Redirect dry-run output to a temp path so the test doesn't litter
    # ingest_output/ in the repo. patch __file__-derived path.
    monkeypatch.setattr(currents_ingest, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        currents_ingest,
        "__file__",
        str(tmp_path / "fake_worker.py"),
    )

    manifest = currents_ingest.ingest_cycle(
        src.name, run_type="f", dry_run=True,
    )
    # fhours 0 and 1 succeed; fhour 2 raises 404 → cycle stops at 1.
    assert manifest["fhours"] == [0, 1]
    assert manifest["source"] == src.name
    assert manifest["run_type"] == "f"
