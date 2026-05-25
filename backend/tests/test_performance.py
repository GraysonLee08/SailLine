"""Pure-function tests for app/services/performance.py.

No DB, no forecast, no polar CSV — a tiny ``FakePolar`` and a constant
wind sampler make the Target-Actual math deterministic. Mirrors the
style of test_heel_stats.py.

Wind convention reminder: ``uv_to_tws_twd`` reads (u east, v north) m/s.
A wind "from the north" (TWD 0) blows toward the south, i.e. u=0,
v=-speed. 10 kt ≈ 5.144 m/s, so (0.0, -5.144) is a 10 kt northerly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.performance import (
    compute_performance_summary,
    evaluate_point,
)


# ─── Fakes ────────────────────────────────────────────────────────────


class FakePolar:
    """Constant target of 6 kt, except below the close-hauled limit
    (TWA < 30°) where the boat 'can't sail' and the polar returns 0 —
    lets us exercise the pinching / target==0 branch deterministically.
    """

    def boat_speed(self, twa_deg, tws_kts, *, hs_m=0.0, density_factor=1.0, margin=1.0):
        return 0.0 if twa_deg < 30.0 else 6.0


NORTHERLY_10KT = (0.0, -5.144)   # u, v in m/s → TWS 10 kt, TWD 0 (from N)


def _t(offset_s: float) -> datetime:
    base = datetime(2026, 5, 20, 18, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_s)


def _pt(t_offset: float, *, sog=6.0, cog=40.0, lat=42.05, lon=-87.6) -> dict:
    return {
        "recorded_at": _t(t_offset),
        "lat": lat,
        "lon": lon,
        "speed_kts": sog,
        "heading_deg": cog,
    }


def _const_wind(_lat, _lon, _t=None):
    return NORTHERLY_10KT


def _no_wind(_lat, _lon, _t=None):
    return None


# ─── evaluate_point ───────────────────────────────────────────────────


def test_evaluate_point_none_on_missing_inputs():
    polar = FakePolar()
    assert evaluate_point(polar, sog_kts=None, cog_deg=40.0, wind_uv=NORTHERLY_10KT) is None
    assert evaluate_point(polar, sog_kts=6.0, cog_deg=None, wind_uv=NORTHERLY_10KT) is None
    assert evaluate_point(polar, sog_kts=6.0, cog_deg=40.0, wind_uv=None) is None


def test_evaluate_point_none_on_calm_wind():
    polar = FakePolar()
    # ~0.2 kt — below the _MIN_TWS_KT floor.
    assert evaluate_point(polar, sog_kts=6.0, cog_deg=40.0, wind_uv=(0.0, -0.1)) is None


def test_evaluate_point_known_target_and_ratio():
    polar = FakePolar()
    out = evaluate_point(polar, sog_kts=3.0, cog_deg=40.0, wind_uv=NORTHERLY_10KT)
    assert out is not None
    assert out["twa"] == pytest.approx(40.0, abs=0.01)
    assert out["tws_kts"] == pytest.approx(10.0, abs=0.05)
    assert out["twd"] == pytest.approx(0.0, abs=0.01)
    assert out["target_kts"] == pytest.approx(6.0)
    assert out["actual_kts"] == pytest.approx(3.0)
    assert out["speed_ratio"] == pytest.approx(0.5)


def test_evaluate_point_upwind_vmg_positive():
    polar = FakePolar()
    out = evaluate_point(polar, sog_kts=5.0, cog_deg=40.0, wind_uv=NORTHERLY_10KT)
    assert out is not None
    # Heading 40° into a northerly = sailing toward the wind → +VMG.
    assert out["actual_vmg"] > 0
    assert out["target_vmg"] > 0
    assert out["vmg_ratio"] == pytest.approx(5.0 / 6.0, abs=1e-3)


def test_evaluate_point_downwind_vmg_negative():
    polar = FakePolar()
    out = evaluate_point(polar, sog_kts=5.0, cog_deg=180.0, wind_uv=NORTHERLY_10KT)
    assert out is not None
    assert out["twa"] == pytest.approx(180.0, abs=0.01)
    # Running dead downwind → made-good is away from the wind → -VMG.
    assert out["actual_vmg"] < 0
    assert out["target_vmg"] < 0
    # Two negatives → positive ratio.
    assert out["vmg_ratio"] == pytest.approx(5.0 / 6.0, abs=1e-3)


def test_evaluate_point_pinching_target_zero_gives_none_ratio():
    polar = FakePolar()
    # TWA 10° < close-hauled limit → polar target 0.
    out = evaluate_point(polar, sog_kts=2.0, cog_deg=10.0, wind_uv=NORTHERLY_10KT)
    assert out is not None
    assert out["target_kts"] == pytest.approx(0.0)
    assert out["speed_ratio"] is None
    assert out["vmg_ratio"] is None


def test_evaluate_point_beam_reach_vmg_ratio_none():
    polar = FakePolar()
    # Beam reach: cos(TWA)→0, target_vmg→0 → vmg_ratio undefined.
    out = evaluate_point(polar, sog_kts=6.0, cog_deg=90.0, wind_uv=NORTHERLY_10KT)
    assert out is not None
    assert out["speed_ratio"] == pytest.approx(1.0)
    assert out["vmg_ratio"] is None


# ─── compute_performance_summary ──────────────────────────────────────


def test_summary_none_on_empty_track():
    assert compute_performance_summary(
        [], wind_sampler=_const_wind, polar=FakePolar(),
    ) is None


def test_summary_none_when_no_wind_coverage():
    track = [_pt(i) for i in range(10)]
    assert compute_performance_summary(
        track, wind_sampler=_no_wind, polar=FakePolar(),
    ) is None


def test_summary_none_when_track_lacks_speed_and_heading():
    track = [
        {"recorded_at": _t(i), "lat": 42.05, "lon": -87.6,
         "speed_kts": None, "heading_deg": None}
        for i in range(10)
    ]
    assert compute_performance_summary(
        track, wind_sampler=_const_wind, polar=FakePolar(),
    ) is None


def test_summary_on_target_track():
    # sog == target (6 kt), upwind heading → ratio 1.0, fully on target.
    track = [_pt(i, sog=6.0, cog=40.0) for i in range(20)]
    out = compute_performance_summary(
        track, wind_sampler=_const_wind, polar=FakePolar(),
    )
    assert out is not None
    assert out["sample_count"] == 20
    assert out["avg_speed_ratio"] == pytest.approx(1.0, abs=1e-3)
    assert out["avg_vmg_efficiency"] == pytest.approx(1.0, abs=1e-3)
    assert out["pct_time_on_target"] == pytest.approx(1.0)
    assert out["avg_target_kts"] == pytest.approx(6.0, abs=1e-3)


def test_summary_off_target_track():
    track = [_pt(i, sog=3.0, cog=40.0) for i in range(20)]
    out = compute_performance_summary(
        track, wind_sampler=_const_wind, polar=FakePolar(),
    )
    assert out is not None
    assert out["avg_speed_ratio"] == pytest.approx(0.5, abs=1e-3)
    assert out["pct_time_on_target"] == pytest.approx(0.0)


def test_summary_vmg_efficiency_none_on_pure_beam_reach():
    # All beam reach → every point excluded from VMG aggregate, but
    # speed ratio is still defined.
    track = [_pt(i, sog=6.0, cog=90.0) for i in range(10)]
    out = compute_performance_summary(
        track, wind_sampler=_const_wind, polar=FakePolar(),
    )
    assert out is not None
    assert out["avg_speed_ratio"] == pytest.approx(1.0, abs=1e-3)
    assert out["avg_vmg_efficiency"] is None


def test_summary_buckets_by_leg():
    track = [_pt(i, sog=6.0, cog=40.0) for i in range(10)]
    passes = [{"mark_index": 0, "ts": _t(5)}]
    out = compute_performance_summary(
        track, wind_sampler=_const_wind, polar=FakePolar(), mark_passes=passes,
    )
    assert out is not None
    legs = {b["leg_index"] for b in out["by_leg"]}
    assert legs == {0, 1}
    for b in out["by_leg"]:
        assert b["sample_count"] > 0


def test_summary_tolerates_string_timestamps():
    track = [
        {"recorded_at": "2026-05-20T18:00:00Z", "lat": 42.05, "lon": -87.6,
         "speed_kts": 6.0, "heading_deg": 40.0},
        {"recorded_at": "2026-05-20T18:00:01+00:00", "lat": 42.05, "lon": -87.6,
         "speed_kts": 6.0, "heading_deg": 40.0},
    ]
    out = compute_performance_summary(
        track, wind_sampler=_const_wind, polar=FakePolar(),
    )
    assert out is not None
    assert out["sample_count"] == 2
