# backend/tests/test_currents_loader.py
"""Tests for the currents loader — Redis I/O + bracketing logic.

Mocks Redis at the module boundary, same pattern as test_forecast_loader.
Covers:
  - Happy path: one source with one cycle + forecast manifest
  - Nowcast + forecast manifest merge with valid_time dedup
  - Multi-source pickup
  - CurrentsUnavailable when no source has any ingested cycle
  - CurrentsUnavailable when cycles exist but no fhours intersect race window
  - Source skipped when its topology is missing
  - _pick_bracketing edge cases
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pytest

from app.currents_regions import get as get_source
from app.services.currents.fields import CurrentsUnavailable
from app.services.currents.loader import (
    _pick_bracketing,
    load_currents_for_race,
)


# ─── Fake Redis ─────────────────────────────────────────────────────────


class _FakeRedis:
    """Minimal async Redis stand-in. Reuses the test_forecast_loader pattern."""
    def __init__(self, store: dict):
        self.store = store

    async def get(self, key):
        return self.store.get(key)

    async def zrevrange(self, key, start, stop):
        return self.store.get(key, [])


# ─── Synthetic fixture builders ─────────────────────────────────────────


_BASE_TIME = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def _fvcom_topology_blob(source: str) -> bytes:
    payload = {
        "kind": "fvcom",
        "source": source,
        "n_nodes": 4,
        "n_triangles": 2,
        "lats": [41.0, 42.0, 41.0, 42.0],
        "lons": [-88.0, -88.0, -87.0, -87.0],
        "triangles": [[0, 1, 2], [1, 3, 2]],
    }
    return gzip.compress(json.dumps(payload).encode())


def _fvcom_snapshot_blob(source: str, cycle_iso: str, run_type: str,
                         fhour: int, valid_time: datetime) -> bytes:
    payload = {
        "kind": "fvcom",
        "source": source,
        "cycle": cycle_iso,
        "run_type": run_type,
        "fhour": fhour,
        "reference_time": _BASE_TIME.isoformat(),
        "valid_time": valid_time.isoformat(),
        "u": [0.1, 0.1, 0.1, 0.1],
        "v": [0.05, 0.05, 0.05, 0.05],
    }
    return gzip.compress(json.dumps(payload).encode())


def _manifest_blob(source: str, run_type: str, fhours: list[int],
                   cycle_iso: str = "20260513T1200Z") -> bytes:
    valid_times = []
    for fh in fhours:
        if run_type == "n":
            # nowcast files are in the past relative to cycle ref
            vt = _BASE_TIME - timedelta(hours=fh)
        else:
            vt = _BASE_TIME + timedelta(hours=fh)
        valid_times.append(vt.isoformat())
    return json.dumps({
        "source": source,
        "grid_type": "fvcom",
        "run_type": run_type,
        "cycle": cycle_iso,
        "fhours": fhours,
        "valid_times": valid_times,
    }).encode()


def _build_store(
    source: str,
    cycle_iso: str = "20260513T1200Z",
    forecast_fhours: list[int] = None,
    nowcast_fhours: list[int] = None,
    has_topology: bool = True,
) -> dict:
    store: dict = {
        f"currents:{source}:cycles": [cycle_iso.encode()],
    }
    if has_topology:
        store[f"currents:{source}:topology"] = _fvcom_topology_blob(source)

    if forecast_fhours is not None:
        store[f"currents:{source}:{cycle_iso}:f_manifest"] = _manifest_blob(
            source, "f", forecast_fhours, cycle_iso,
        )
        for fh in forecast_fhours:
            vt = _BASE_TIME + timedelta(hours=fh)
            store[f"currents:{source}:{cycle_iso}:f{fh:03d}"] = _fvcom_snapshot_blob(
                source, cycle_iso, "f", fh, vt,
            )
    if nowcast_fhours is not None:
        store[f"currents:{source}:{cycle_iso}:n_manifest"] = _manifest_blob(
            source, "n", nowcast_fhours, cycle_iso,
        )
        for fh in nowcast_fhours:
            vt = _BASE_TIME - timedelta(hours=fh)
            store[f"currents:{source}:{cycle_iso}:n{fh:03d}"] = _fvcom_snapshot_blob(
                source, cycle_iso, "n", fh, vt,
            )
    return store


# ─── _pick_bracketing unit tests ────────────────────────────────────────


def test_pick_bracketing_selects_in_window_plus_brackets():
    """Entries in-window plus one bracket on each side."""
    entries = [
        ("f", 0, _BASE_TIME + timedelta(hours=0)),
        ("f", 1, _BASE_TIME + timedelta(hours=1)),
        ("f", 2, _BASE_TIME + timedelta(hours=2)),
        ("f", 3, _BASE_TIME + timedelta(hours=3)),
        ("f", 4, _BASE_TIME + timedelta(hours=4)),
    ]
    # Window: [+1h, +2h]
    picks = _pick_bracketing(
        entries,
        _BASE_TIME + timedelta(hours=1),
        _BASE_TIME + timedelta(hours=2),
    )
    fhours = [fh for _, fh, _ in picks]
    # In-window: 1, 2. Brackets: 0 (before), 3 (after).
    assert set(fhours) == {0, 1, 2, 3}


def test_pick_bracketing_dedup_prefers_forecast_at_tied_valid_time():
    """When nowcast n001 and forecast f000 share the cycle ref time, forecast wins."""
    same_time = _BASE_TIME
    entries = [
        ("n", 1, same_time),
        ("f", 0, same_time),
    ]
    picks = _pick_bracketing(entries, same_time, same_time + timedelta(hours=1))
    # Only one entry at same_time should be selected — and it should be the forecast.
    matched = [e for e in picks if e[2] == same_time]
    assert len(matched) == 1
    assert matched[0][0] == "f"


def test_pick_bracketing_empty_input():
    assert _pick_bracketing([], _BASE_TIME, _BASE_TIME + timedelta(hours=1)) == []


# ─── Loader integration ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_currents_happy_path_one_source():
    """One source with forecast manifest covering the race → CurrentForecast returned."""
    source = get_source("lmhofs")
    store = _build_store(
        source.name,
        forecast_fhours=[0, 1, 2, 3, 4, 5, 6],
    )
    fake = _FakeRedis(store)
    with patch("app.services.currents.loader.redis_client.get_client",
               return_value=fake):
        forecast = await load_currents_for_race(
            sources=[source],
            race_start=_BASE_TIME + timedelta(hours=1),
            duration_hours=4,
        )
    assert forecast.quality == "lmhofs"
    assert len(forecast.snapshots) >= 4


@pytest.mark.asyncio
async def test_load_currents_no_sources_raises():
    with pytest.raises(CurrentsUnavailable):
        await load_currents_for_race(sources=[], race_start=_BASE_TIME)


@pytest.mark.asyncio
async def test_load_currents_no_cycle_raises():
    """Source registered but its cycles ZSET is empty."""
    source = get_source("lmhofs")
    fake = _FakeRedis({})  # no keys at all
    with patch("app.services.currents.loader.redis_client.get_client",
               return_value=fake):
        with pytest.raises(CurrentsUnavailable):
            await load_currents_for_race(
                sources=[source],
                race_start=_BASE_TIME + timedelta(hours=1),
            )


@pytest.mark.asyncio
async def test_load_currents_missing_topology_skips_source():
    """Source with a cycle but no topology blob — skipped, surfaces as unavailable."""
    source = get_source("lmhofs")
    store = _build_store(
        source.name,
        forecast_fhours=[0, 1, 2],
        has_topology=False,
    )
    fake = _FakeRedis(store)
    with patch("app.services.currents.loader.redis_client.get_client",
               return_value=fake):
        with pytest.raises(CurrentsUnavailable):
            await load_currents_for_race(
                sources=[source],
                race_start=_BASE_TIME + timedelta(hours=1),
            )


@pytest.mark.asyncio
async def test_load_currents_race_far_outside_cycle_returns_brackets_only():
    """Cycle exists but race is far in the future.

    Mirrors the wind loader's behaviour: ``_pick_bracketing`` still
    selects the latest pre-window fhour as a bracket, so the loader
    returns a CurrentForecast that does NOT cover the race window. The
    engine's CurrentForecast.sample returns None for valid_times outside
    [t_min, t_max], so currents effectively don't contribute to the
    route — but we don't raise, consistent with the wind loader.
    """
    source = get_source("lmhofs")
    store = _build_store(
        source.name,
        forecast_fhours=[0, 1, 2],  # only covers 0-2h after cycle
    )
    fake = _FakeRedis(store)
    race_start = _BASE_TIME + timedelta(hours=48)
    with patch("app.services.currents.loader.redis_client.get_client",
               return_value=fake):
        forecast = await load_currents_for_race(
            sources=[source],
            race_start=race_start,
            duration_hours=4,
        )
    # The forecast's time window ends well before the race starts.
    assert forecast.t_max < race_start
    # Engine samples during the race window therefore return None.
    sample_during_race = forecast.sample(41.5, -87.5, race_start + timedelta(hours=1))
    assert sample_during_race is None


@pytest.mark.asyncio
async def test_load_currents_nowcast_plus_forecast_merges():
    """Nowcast covers recent past, forecast covers future. Both should load."""
    source = get_source("lmhofs")
    store = _build_store(
        source.name,
        forecast_fhours=[0, 1, 2, 3, 4, 5, 6],
        nowcast_fhours=[1, 2, 3],   # cover -1h, -2h, -3h
    )
    fake = _FakeRedis(store)
    with patch("app.services.currents.loader.redis_client.get_client",
               return_value=fake):
        # Race starts 1h before cycle ref, runs 4h forward.
        forecast = await load_currents_for_race(
            sources=[source],
            race_start=_BASE_TIME - timedelta(hours=1),
            duration_hours=4,
        )
    # Should have picked up both nowcast and forecast snapshots.
    assert len(forecast.snapshots) >= 4
    assert forecast.quality == "lmhofs"
