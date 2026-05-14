"""Pure-function tests for app/services/wind_snapshot.py.

A tiny fake ``_ForecastLike`` stands in for WindForecast — no need to
spin up real GRIB data or asyncpg. We exercise the snapshot shape,
out-of-bounds handling, summary stats, and a few size sanity checks
so growth in the schema doesn't sneak past review.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.services.wind_snapshot import (
    DEFAULT_DT_S,
    DEFAULT_GRID_DEG,
    MAX_GRID_CELLS,
    MAX_TIME_STEPS,
    marks_bbox,
    snapshot_forecast,
    summarise_snapshot,
    uv_to_dir_speed_kt,
)


T0 = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)


# ─── Fake forecast ────────────────────────────────────────────────────


@dataclass
class FakeForecast:
    """Returns a constant (u, v) inside its bbox; None outside.

    The default constant is a 10 kt westerly — meteorologists call that
    "from the west", which in u/v terms is u > 0 (eastward component
    positive). 10 kt ≈ 5.144 m/s.
    """
    bbox: tuple[float, float, float, float]   # (min_lat, max_lat, min_lon, max_lon)
    u: float = 5.144   # ~10 kt eastward
    v: float = 0.0
    quality: str = "hybrid"

    def sample(
        self, lat: float, lon: float, valid_time: Optional[datetime] = None,
    ) -> Optional[tuple[float, float]]:
        min_lat, max_lat, min_lon, max_lon = self.bbox
        if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
            return None
        return (self.u, self.v)


# ─── Bbox ─────────────────────────────────────────────────────────────


def test_marks_bbox_pads_in_all_directions():
    marks = [
        {"lat": 42.0, "lon": -87.7},
        {"lat": 42.1, "lon": -87.5},
    ]
    bbox = marks_bbox(marks, pad_deg=0.05)
    assert bbox is not None
    min_lat, min_lon, max_lat, max_lon = bbox
    assert min_lat == pytest.approx(41.95, abs=1e-6)
    assert max_lat == pytest.approx(42.15, abs=1e-6)
    assert min_lon == pytest.approx(-87.75, abs=1e-6)
    assert max_lon == pytest.approx(-87.45, abs=1e-6)


def test_marks_bbox_ignores_malformed_entries():
    marks = [
        {"lat": 42.0, "lon": -87.7},
        {"name": "missing"},
        {"lat": "bogus", "lon": -87.7},
    ]
    bbox = marks_bbox(marks, pad_deg=0)
    assert bbox == (42.0, -87.7, 42.0, -87.7)


def test_marks_bbox_returns_none_when_no_usable_marks():
    assert marks_bbox([]) is None
    assert marks_bbox([{"foo": "bar"}]) is None


# ─── Snapshot shape ──────────────────────────────────────────────────


def test_snapshot_basic_shape_and_keys():
    fc = FakeForecast(bbox=(41.0, 43.0, -89.0, -86.0))
    snap = snapshot_forecast(
        fc,
        bbox=(41.95, -87.75, 42.15, -87.45),
        t_start=T0,
        t_end=T0 + timedelta(hours=2),
    )
    # Required keys.
    for key in (
        "bbox", "grid_deg", "lats", "lons", "t_start", "t_end",
        "dt_s", "times", "source", "u_mps", "v_mps",
    ):
        assert key in snap, f"missing key: {key}"
    # Source pulled from forecast.quality.
    assert snap["source"] == "hybrid"
    # Defaults applied.
    assert snap["grid_deg"] == DEFAULT_GRID_DEG
    assert snap["dt_s"] == DEFAULT_DT_S
    # Grid shape consistency.
    T = len(snap["times"])
    M = len(snap["lats"])
    N = len(snap["lons"])
    assert len(snap["u_mps"]) == T
    assert all(len(slice_) == M for slice_ in snap["u_mps"])
    assert all(
        len(row) == N
        for slice_ in snap["u_mps"]
        for row in slice_
    )


def test_snapshot_values_match_forecast():
    fc = FakeForecast(bbox=(41.0, 43.0, -89.0, -86.0), u=5.144, v=0.0)
    snap = snapshot_forecast(
        fc,
        bbox=(41.95, -87.75, 42.15, -87.45),
        t_start=T0,
        t_end=T0 + timedelta(hours=1),
    )
    # Every cell is in-bounds, so every entry should be (5.144, 0.0).
    for slice_ in snap["u_mps"]:
        for row in slice_:
            assert all(u == pytest.approx(5.144) for u in row)
    for slice_ in snap["v_mps"]:
        for row in slice_:
            assert all(v == 0.0 for v in row)


def test_snapshot_records_null_outside_forecast_bbox():
    # Forecast covers a moderate box centred on 42N/-87.5W; race bbox
    # extends well outside it on the south/east edges so some grid
    # cells fall inside and some fall outside.
    fc = FakeForecast(bbox=(41.5, 42.5, -88.0, -87.0))
    snap = snapshot_forecast(
        fc,
        bbox=(41.0, -88.5, 43.0, -86.5),    # wider than forecast
        t_start=T0,
        t_end=T0 + timedelta(minutes=30),
        grid_deg=0.5,
    )
    has_null = False
    has_value = False
    for slice_ in snap["u_mps"]:
        for row in slice_:
            for u in row:
                if u is None:
                    has_null = True
                else:
                    has_value = True
    assert has_null, "expected at least some null cells outside forecast"
    assert has_value, "expected at least some valued cells inside forecast"


def test_snapshot_respects_grid_cell_cap():
    # Wide bbox at fine resolution would explode the grid. The capper
    # should hold lats × lons under MAX_GRID_CELLS.
    fc = FakeForecast(bbox=(-90.0, 90.0, -180.0, 180.0))
    snap = snapshot_forecast(
        fc,
        bbox=(20.0, -125.0, 50.0, -65.0),
        t_start=T0,
        t_end=T0 + timedelta(minutes=15),
        grid_deg=0.01,    # absurdly fine
    )
    assert len(snap["lats"]) * len(snap["lons"]) <= MAX_GRID_CELLS


def test_snapshot_respects_time_step_cap():
    fc = FakeForecast(bbox=(41.0, 43.0, -89.0, -86.0))
    snap = snapshot_forecast(
        fc,
        bbox=(41.95, -87.75, 42.15, -87.45),
        t_start=T0,
        t_end=T0 + timedelta(days=10),   # 1000+ steps without the cap
        dt_s=60,
    )
    assert len(snap["times"]) <= MAX_TIME_STEPS


def test_snapshot_rejects_inverted_time_window():
    fc = FakeForecast(bbox=(41.0, 43.0, -89.0, -86.0))
    with pytest.raises(ValueError):
        snapshot_forecast(
            fc,
            bbox=(41.95, -87.75, 42.15, -87.45),
            t_start=T0,
            t_end=T0,
        )


def test_snapshot_is_json_serialisable():
    fc = FakeForecast(bbox=(41.0, 43.0, -89.0, -86.0))
    snap = snapshot_forecast(
        fc,
        bbox=(41.95, -87.75, 42.15, -87.45),
        t_start=T0,
        t_end=T0 + timedelta(hours=2),
    )
    # Round-trip through json.dumps to confirm no datetimes leak.
    s = json.dumps(snap)
    parsed = json.loads(s)
    assert parsed["t_start"] == snap["t_start"]


# ─── uv_to_dir_speed_kt ──────────────────────────────────────────────


def test_uv_pure_westerly_reads_as_270_from_or_close():
    """Westerly: wind from the west, blowing east. u > 0, v ~ 0.

    Met convention: 270° = wind from the west.
    """
    d, s = uv_to_dir_speed_kt(5.144, 0.0)
    assert d == pytest.approx(270.0, abs=0.5)
    assert s == pytest.approx(10.0, abs=0.1)


def test_uv_pure_northerly_reads_as_360_or_0():
    """Northerly: wind from the north, blowing south. v < 0.

    Met convention: 0°/360° = wind from the north.
    """
    d, _ = uv_to_dir_speed_kt(0.0, -5.144)
    # Allow 0 or 360 here — both are equivalent.
    assert d % 360.0 == pytest.approx(0.0, abs=0.5) or d == pytest.approx(360.0, abs=0.5)


def test_uv_pure_southerly_reads_as_180():
    d, _ = uv_to_dir_speed_kt(0.0, 5.144)
    assert d == pytest.approx(180.0, abs=0.5)


# ─── summarise_snapshot ──────────────────────────────────────────────


def test_summary_constant_wind_has_zero_dir_range():
    fc = FakeForecast(bbox=(41.0, 43.0, -89.0, -86.0), u=5.144, v=0.0)
    snap = snapshot_forecast(
        fc,
        bbox=(41.95, -87.75, 42.15, -87.45),
        t_start=T0,
        t_end=T0 + timedelta(hours=2),
    )
    summ = summarise_snapshot(snap)
    assert summ["mean_speed_kt"] == pytest.approx(10.0, abs=0.1)
    assert summ["max_speed_kt"] == pytest.approx(10.0, abs=0.1)
    assert summ["dir_range_deg"] == pytest.approx(0.0, abs=0.5)
    assert summ["cell_coverage"] == pytest.approx(1.0, abs=1e-6)


def test_summary_handles_partial_coverage():
    fc = FakeForecast(bbox=(41.5, 42.5, -88.0, -87.0), u=5.144, v=0.0)
    snap = snapshot_forecast(
        fc,
        bbox=(41.0, -88.5, 43.0, -86.5),
        t_start=T0,
        t_end=T0 + timedelta(minutes=15),
        grid_deg=0.5,
    )
    summ = summarise_snapshot(snap)
    assert 0.0 < summ["cell_coverage"] < 1.0


def test_summary_all_null_returns_no_numbers():
    fc = FakeForecast(bbox=(42.0, 42.001, -87.7, -87.699))   # tiny
    snap = snapshot_forecast(
        fc,
        bbox=(43.0, -86.0, 44.0, -85.0),    # entirely outside forecast
        t_start=T0,
        t_end=T0 + timedelta(minutes=15),
        grid_deg=0.5,
    )
    summ = summarise_snapshot(snap)
    assert summ["mean_speed_kt"] is None
    assert summ["max_speed_kt"] is None
    assert summ["cell_coverage"] == 0.0


def test_summary_dir_mean_handles_wraparound():
    """Synthesise a snapshot whose values straddle north: half at 359°,
    half at 1°. Naive arithmetic mean would say 180°; vector mean must
    return ~0°.
    """
    snap = {
        "u_mps": [[[
            # 359° from = blowing toward 179° = u=sin(179°), v=cos(179°)
            math.sin(math.radians(179.0)) * 5.0,
            math.sin(math.radians(181.0)) * 5.0,
        ]]],
        "v_mps": [[[
            math.cos(math.radians(179.0)) * 5.0,
            math.cos(math.radians(181.0)) * 5.0,
        ]]],
    }
    summ = summarise_snapshot(snap)
    # Mean direction should be near 0/360, not near 180.
    d = summ["mean_dir_deg"] % 360.0
    assert d < 5.0 or d > 355.0
