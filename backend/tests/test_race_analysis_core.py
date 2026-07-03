"""Tests for race_analysis: geo, preprocess, start, legs, maneuvers,
shifts (spec sections 1.0–1.4). Pure-function tests, no I/O, no mocks
beyond simple fake samplers.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from app.services.race_analysis.geo import (
    angle_diff,
    circular_mean_deg,
    signed_twa_deg,
    uv_to_tws_twd,
)
from app.services.race_analysis.legs import Leg, segment_legs
from app.services.race_analysis.maneuvers import (
    detect_maneuvers,
    summarize_maneuvers,
)
from app.services.race_analysis.preprocess import (
    AnalysisPoint,
    build_wind_sampler,
    clean_track,
)
from app.services.race_analysis.shifts import analyze_shifts
from app.services.race_analysis.start import analyze_start, resolve_line_bearing

T0 = datetime(2026, 7, 1, 18, 0, 0, tzinfo=timezone.utc)


# ─── Track builders ───────────────────────────────────────────────────


def _row(t_offset_s, lat, lon, sog=5.0, cog=0.0, acc=5.0):
    return {
        "recorded_at": T0 + timedelta(seconds=t_offset_s),
        "lat": lat, "lon": lon,
        "speed_kts": sog, "heading_deg": cog, "gps_acc_m": acc,
    }


def _northerly_wind(lat, lon, t):
    """Constant 10 kt from due north."""
    return 0.0, 10.0


def _beat_track(duration_s=300, tack_at_s=150, sog=6.0):
    """1 Hz beat: port tack (COG 45) then starboard (COG 315) under a
    northerly. Positions crawl north so distance math stays sane."""
    rows = []
    for s in range(duration_s):
        cog = 45.0 if s < tack_at_s else 315.0
        rows.append(_row(s, 42.0 + s * 2e-5, -87.6, sog=sog, cog=cog))
    return rows


# ─── geo ──────────────────────────────────────────────────────────────


def test_angle_diff_wraps():
    assert angle_diff(350.0, 10.0) == -20.0
    assert angle_diff(10.0, 350.0) == 20.0
    assert angle_diff(180.0, 0.0) == -180.0  # [-180, 180) convention


def test_signed_twa_convention():
    # Wind from N, heading NE → wind over the port bow → negative.
    assert signed_twa_deg(0.0, 45.0) == -45.0
    # Heading NW → starboard tack → positive.
    assert signed_twa_deg(0.0, 315.0) == 45.0


def test_uv_to_tws_twd_matches_spec_formula():
    # u=-5.144, v=0 → wind blowing toward west → FROM east (90°), 10 kt.
    tws, twd = uv_to_tws_twd(-5.144, 0.0)
    assert abs(tws - 10.0) < 0.01
    assert abs(twd - 90.0) < 0.01
    # Spec formula: TWD = atan2(-u, -v) — same result.
    assert abs((math.degrees(math.atan2(5.144, 0.0)) % 360.0) - twd) < 0.01


def test_circular_mean_wraps_north():
    m = circular_mean_deg([350.0, 10.0])
    assert m is not None and (m < 1.0 or m > 359.0)


# ─── preprocess (1.0) ─────────────────────────────────────────────────


def test_clean_track_drops_bad_accuracy_and_speed():
    rows = [
        _row(0, 42.0, -87.6),
        _row(1, 42.0, -87.6, acc=25.0),        # gps_acc > 15 → dropped
        _row(2, 42.0, -87.6, sog=30.0),        # SOG > 25 → dropped
        _row(3, 42.0, -87.6),
    ]
    pts = clean_track(rows)
    assert len(pts) == 2


def test_clean_track_median_smooths_sog_spike():
    rows = [_row(s, 42.0, -87.6, sog=5.0) for s in range(7)]
    rows[3]["speed_kts"] = 15.0  # one-sample spike survives the filter
    pts = clean_track(rows)
    # 5-sample median swallows the single spike.
    assert all(p.sog_kts == 5.0 for p in pts)


def test_clean_track_cog_median_survives_north_wrap():
    cogs = [358.0, 359.0, 1.0, 2.0, 3.0]
    rows = [_row(s, 42.0, -87.6, cog=c) for s, c in enumerate(cogs)]
    pts = clean_track(rows)
    # Middle sample's window spans the seam; naive median would give ~180.
    assert min(pts[2].cog_deg, 360.0 - pts[2].cog_deg) < 5.0


def test_clean_track_annotates_twa():
    rows = _beat_track(duration_s=20, tack_at_s=100)
    pts = clean_track(rows, wind_at=_northerly_wind)
    assert all(p.twd_deg == 0.0 for p in pts)
    assert all(p.twa_deg == -45.0 for p in pts)  # COG 45 under N wind = port


def _snapshot_two_times():
    """2×2 grid, two time steps: u goes 0 → -5.144 (E'ly building)."""
    return {
        "lats": [41.9, 42.1],
        "lons": [-87.7, -87.5],
        "times": ["2026-07-01T18:00:00+00:00", "2026-07-01T19:00:00+00:00"],
        "u_mps": [
            [[0.0, 0.0], [0.0, 0.0]],
            [[-5.144, -5.144], [-5.144, -5.144]],
        ],
        "v_mps": [
            [[-5.144, -5.144], [-5.144, -5.144]],
            [[0.0, 0.0], [0.0, 0.0]],
        ],
    }


def test_wind_sampler_interpolates_linearly_in_time():
    sampler, source = build_wind_sampler(_snapshot_two_times())
    assert sampler is not None and source == "model"
    mid = sampler(42.0, -87.6, T0 + timedelta(minutes=30))
    assert mid is not None
    twd, tws = mid
    # u,v both -2.572 at the midpoint → wind FROM 45°.
    assert abs(twd - 45.0) < 1.0


def test_wind_sampler_returns_none_outside_window():
    sampler, _ = build_wind_sampler(_snapshot_two_times())
    assert sampler(42.0, -87.6, T0 - timedelta(hours=2)) is None


def test_wind_sampler_blends_buoy_twd_offset():
    obs = {
        "stations": [{
            "source": "ndbc", "station_id": "45x", "lat": 42.0, "lon": -87.6,
            "distance_km": 5.0,
            "obs": [
                {"ts": (T0 + timedelta(minutes=m)).isoformat(), "wdir_deg": 10.0}
                for m in (0, 5, 10, 15)
            ],
        }],
    }
    snap = {
        "lats": [41.9, 42.1], "lons": [-87.7, -87.5],
        "times": ["2026-07-01T18:00:00+00:00", "2026-07-01T19:00:00+00:00"],
        # Constant northerly (v=-5.144 → from N).
        "u_mps": [[[0.0, 0.0], [0.0, 0.0]]] * 2,
        "v_mps": [[[-5.144, -5.144], [-5.144, -5.144]]] * 2,
    }
    sampler, source = build_wind_sampler(snap, obs)
    assert source == "blended"
    twd, _ = sampler(42.0, -87.6, T0 + timedelta(minutes=5))
    assert abs(twd - 10.0) < 1.0  # model 0° + buoy offset +10°


def test_wind_sampler_ignores_distant_buoy():
    obs = {
        "stations": [{
            "lat": 43.0, "lon": -86.0, "distance_km": 100.0,
            "obs": [{"ts": T0.isoformat(), "wdir_deg": 90.0}] * 5,
        }],
    }
    _, source = build_wind_sampler(_snapshot_two_times(), obs)
    assert source == "model"


# ─── start (1.1) ──────────────────────────────────────────────────────

# Start mark at origin-ish; line bearing 90 → E-W line. Mark 1 due
# north → course side is north.
_MARKS = [
    {"lat": 42.0, "lon": -87.6, "name": "Start"},
    {"lat": 42.02, "lon": -87.6, "name": "W"},
]


def _start_track(gun_offset_cross_s=10):
    """Boat runs due north at 6 kt, crossing the line ~gun+10 s.
    6 kt ≈ 3.09 m/s ≈ 2.78e-5 °lat per second."""
    rows = []
    deg_per_s = 3.087 / 111_320.0
    for s in range(-120, 240):
        lat = 42.0 + (s - gun_offset_cross_s) * deg_per_s
        rows.append(_row(s + 120, lat, -87.6, sog=6.0, cog=0.0))
    return rows


def test_start_analysis_square_line():
    gun = T0 + timedelta(seconds=120)
    rows = _start_track()
    pts = clean_track(rows, wind_at=_northerly_wind)
    # Re-anchor timestamps: rows used offsets 0..360, gun at 120.
    out = analyze_start(
        pts, marks=_MARKS, gun=gun, line_bearing_deg=90.0,
        wind_at=_northerly_wind,
    )
    assert out is not None
    assert out["ocs"] is False
    # ~10 s late over the line at ~3.1 m/s → ~31 m back at the gun.
    assert 20 <= out["distance_to_line_at_gun_m"] <= 45
    assert out["time_to_cross_s"] is not None
    assert 5 <= out["time_to_cross_s"] <= 15
    # Northerly on an E-W line = square.
    assert out["bias_deg"] == 0.0
    assert out["favored_end"] == "even"


def test_start_bias_sign():
    gun = T0 + timedelta(seconds=120)
    pts = clean_track(_start_track(), wind_at=lambda la, lo, t: (10.0, 10.0))
    out = analyze_start(
        pts, marks=_MARKS, gun=gun, line_bearing_deg=90.0,
        wind_at=lambda la, lo, t: (10.0, 10.0),
    )
    assert out is not None
    # Wind right of north → end A (east, at the line bearing) favored.
    assert out["bias_deg"] == 10.0
    assert out["favored_end"] == "A"


def test_start_returns_none_without_bearing_or_gun():
    pts = clean_track(_start_track())
    assert analyze_start(
        pts, marks=_MARKS, gun=None, line_bearing_deg=90.0, wind_at=None,
    ) is None
    assert analyze_start(
        pts, marks=_MARKS, gun=T0, line_bearing_deg=None, wind_at=None,
    ) is None


def test_resolve_line_bearing_priority():
    assert resolve_line_bearing(
        override=100.0, resolved=50.0, marks=_MARKS, gun=T0,
        wind_at=_northerly_wind,
    ) == 100.0
    assert resolve_line_bearing(
        override=None, resolved=50.0, marks=_MARKS, gun=T0,
        wind_at=_northerly_wind,
    ) == 50.0
    # Derived: TWD 0 + 90.
    assert resolve_line_bearing(
        override=None, resolved=None, marks=_MARKS, gun=T0,
        wind_at=_northerly_wind,
    ) == 90.0


# ─── legs (1.2) ───────────────────────────────────────────────────────


def _passes(*offsets_and_indices):
    return [
        {"mark_index": idx, "ts": (T0 + timedelta(seconds=off)).isoformat(),
         "lat": 42.0, "lon": -87.6}
        for off, idx in offsets_and_indices
    ]


def test_segment_legs_classifies_upwind_and_run():
    marks = [
        {"lat": 42.0, "lon": -87.6},    # start
        {"lat": 42.02, "lon": -87.6},   # windward (due N; northerly → upwind)
        {"lat": 42.0, "lon": -87.6},    # leeward = start (run back)
    ]
    rows = []
    # Leg 1: beat north 0..300 s; leg 2: run south 300..600 s.
    for s in range(0, 300):
        rows.append(_row(s, 42.0 + s * 6.7e-5 / 1.0, -87.6, sog=5.0,
                         cog=45.0 if (s // 60) % 2 == 0 else 315.0))
    for s in range(300, 600):
        rows.append(_row(s, 42.02 - (s - 300) * 6.7e-5, -87.6, sog=6.0, cog=180.0))
    pts = clean_track(rows, wind_at=_northerly_wind)
    legs = segment_legs(
        pts, marks=marks,
        mark_passes=_passes((0, 0), (300, 1), (600, 2)),
    )
    assert len(legs) == 2
    assert legs[0].type == "upwind"
    assert legs[1].type == "run"
    assert legs[0].n == 1
    assert legs[1].elapsed_s == 300.0
    # Run is dead straight → sailed_ratio ≈ 1.
    assert legs[1].sailed_ratio is not None
    assert 0.95 <= legs[1].sailed_ratio <= 1.1
    # Beat zigzags → sailed_ratio > 1.
    assert legs[0].sailed_ratio is None or legs[0].sailed_ratio >= 1.0


def test_segment_legs_needs_two_passes():
    pts = clean_track(_beat_track())
    assert segment_legs(pts, marks=_MARKS, mark_passes=_passes((0, 0))) == []


# ─── maneuvers (1.3) ──────────────────────────────────────────────────


def test_detect_tack_and_cost():
    rows = _beat_track(duration_s=300, tack_at_s=150, sog=6.0)
    # Slow water for 10 s after the tack.
    for s in range(150, 160):
        rows[s]["speed_kts"] = 3.0
    pts = clean_track(rows, wind_at=_northerly_wind)
    out = detect_maneuvers(pts, loa_m=10.0)
    assert len(out) == 1
    m = out[0]
    assert m.kind == "tack"
    assert m.from_tack == "port"        # COG 45 under a northerly
    assert m.cog_delta_deg is not None and m.cog_delta_deg > 60
    # ~10 s × ~3 kt deficit ≈ 30 kt·s ≈ 15.4 m ≈ 1.5 boatlengths.
    assert m.loss_boatlengths is not None
    assert 0.8 <= m.loss_boatlengths <= 2.5


def test_detect_gybe():
    rows = []
    for s in range(300):
        cog = 135.0 if s < 150 else 225.0  # broad reach → broad reach
        rows.append(_row(s, 42.0 - s * 2e-5, -87.6, sog=6.0, cog=cog))
    pts = clean_track(rows, wind_at=_northerly_wind)
    out = detect_maneuvers(pts, loa_m=10.0)
    assert len(out) == 1
    assert out[0].kind == "gybe"


def test_no_maneuver_on_momentary_wobble():
    rows = _beat_track(duration_s=300, tack_at_s=1000)
    # 3-second COG wobble across head-to-wind, then back.
    for s in range(150, 153):
        rows[s]["heading_deg"] = 335.0
    pts = clean_track(rows, wind_at=_northerly_wind)
    assert detect_maneuvers(pts, loa_m=10.0) == []


def test_summarize_maneuvers_shape():
    rows = _beat_track(duration_s=300, tack_at_s=150)
    pts = clean_track(rows, wind_at=_northerly_wind)
    out = summarize_maneuvers(detect_maneuvers(
        pts, loa_m=10.0, leg_bounds=[(1, pts[0].t, pts[-1].t)],
    ))
    assert out["tacks"]["count"] == 1
    assert out["gybes"]["count"] == 0
    assert "1" in out["by_leg"]


# ─── shifts (1.4) ─────────────────────────────────────────────────────


def _upwind_leg_with_oscillation(headed_from_s=150, duration_s=300):
    """Starboard-tack leg (COG 315 under ~N wind). TWD +10 (lift) for
    the first half, −10 (header) after ``headed_from_s``."""
    pts = []
    for s in range(duration_s):
        twd = 10.0 if s < headed_from_s else -10.0
        pts.append(AnalysisPoint(
            t=T0 + timedelta(seconds=s), lat=42.0 + s * 2e-5, lon=-87.6,
            sog_kts=6.0, cog_deg=315.0,
            twd_deg=twd % 360.0, tws_kts=10.0,
            twa_deg=signed_twa_deg(twd, 315.0),
        ))
    return Leg(
        n=1, type="upwind", from_mark_index=0, to_mark_index=1,
        start_ts=pts[0].t, end_ts=pts[-1].t,
        elapsed_s=float(duration_s), distance_sailed_nm=0.5, rhumb_nm=0.4,
        sailed_ratio=1.25, rhumb_bearing_deg=0.0, avg_sog_kts=6.0,
        avg_vmg_to_mark_kts=4.0, mean_twd_deg=0.0, mean_tws_kts=10.0,
        points=pts,
    )


def test_shift_exploitation_lifted_and_headed():
    out = analyze_shifts(_upwind_leg_with_oscillation())
    assert out is not None
    # Half the leg lifted, half headed (±10° about the 0° mean).
    assert 0.4 <= out["pct_time_lifted"] <= 0.6
    assert 0.4 <= out["pct_time_headed"] <= 0.6
    # 150 s continuous header with no tack → one missed shift.
    assert len(out["missed_shifts"]) == 1
    assert out["missed_shifts"][0]["duration_s"] > 90
    assert abs(out["missed_shifts"][0]["mean_magnitude_deg"] - 10.0) < 3.0


def test_shift_analysis_skips_non_upwind():
    leg = _upwind_leg_with_oscillation()
    leg.type = "reach"
    assert analyze_shifts(leg) is None
