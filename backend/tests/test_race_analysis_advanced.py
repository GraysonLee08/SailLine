"""Tests for race_analysis: laylines, roundings, leeway, wind timeline,
call replay, and condition signatures (spec sections 1.5–1.9 + the
playbook matcher's scoring).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.race_analysis.calls import replay_calls
from app.services.race_analysis.geo import signed_twa_deg
from app.services.race_analysis.laylines import analyze_laylines, optimal_twa
from app.services.race_analysis.leeway import analyze_leeway
from app.services.race_analysis.legs import Leg
from app.services.race_analysis.maneuvers import Maneuver
from app.services.race_analysis.preprocess import AnalysisPoint
from app.services.race_analysis.roundings import analyze_roundings
from app.services.race_analysis.signature import (
    MATCH_THRESHOLD,
    best_match,
    match_score,
    signature_text,
)
from app.services.race_analysis.wind_timeline import (
    build_wind_timeline,
    detect_events,
    forecast_rows,
    summarize_conditions,
)

T0 = datetime(2026, 7, 1, 18, 0, 0, tzinfo=timezone.utc)


class _FakePolar:
    """boat_speed peaks so the VMG optimum lands at exactly 45° upwind
    and ~150° downwind."""

    def boat_speed(self, twa_deg: float, tws_kts: float, **_) -> float:
        if twa_deg < 40.0:
            return 0.0
        if twa_deg <= 90.0:
            return 6.0
        return 7.0


def _pt(s, lat=42.0, lon=-87.6, sog=6.0, cog=0.0, twd=0.0, tws=10.0):
    return AnalysisPoint(
        t=T0 + timedelta(seconds=s), lat=lat, lon=lon,
        sog_kts=sog, cog_deg=cog, twd_deg=twd, tws_kts=tws,
        twa_deg=signed_twa_deg(twd, cog),
    )


# ─── laylines (1.5) ───────────────────────────────────────────────────


def test_optimal_twa_upwind_sweep():
    # Speed is flat 6.0 from 40°..90° → VMG max at the narrowest angle.
    assert optimal_twa(_FakePolar(), 10.0) == 40.0


def test_optimal_twa_downwind_sweep():
    # Flat 7.0 past 90° → |VMG| max at the deepest angle in the sweep.
    twa = optimal_twa(_FakePolar(), 10.0, downwind=True)
    assert twa is not None and twa >= 175.0


def _upwind_leg(points):
    return Leg(
        n=1, type="upwind", from_mark_index=0, to_mark_index=1,
        start_ts=points[0].t, end_ts=points[-1].t,
        elapsed_s=(points[-1].t - points[0].t).total_seconds(),
        distance_sailed_nm=0.5, rhumb_nm=0.4, sailed_ratio=1.25,
        rhumb_bearing_deg=0.0, avg_sog_kts=6.0, avg_vmg_to_mark_kts=4.0,
        mean_twd_deg=0.0, mean_tws_kts=10.0, points=points,
    )


def test_overstand_detected():
    # Mark due north-ish; boat sits ~1 km EAST of the mark with a
    # northerly → bearing to mark ≈ 270°, 90° off the wind, way past
    # the 40° layline → large overstand.
    marks = [{"lat": 42.0, "lon": -87.6}, {"lat": 42.0, "lon": -87.612}]
    pts = [_pt(s, lat=42.0, lon=-87.6, cog=315.0) for s in range(60)]
    out = analyze_laylines(
        _upwind_leg(pts), polar=_FakePolar(), marks=marks,
        maneuvers=[], loa_m=10.0,
    )
    assert out is not None
    assert out["optimal_upwind_twa_deg"] == 40.0
    assert out["overstand_m"] > 300


def test_no_overstand_inside_laylines():
    # Boat due south of the mark → bearing 0° = dead upwind → inside.
    marks = [{"lat": 42.0, "lon": -87.6}, {"lat": 42.01, "lon": -87.6}]
    pts = [_pt(s, lat=42.0, lon=-87.6, cog=315.0) for s in range(60)]
    out = analyze_laylines(
        _upwind_leg(pts), polar=_FakePolar(), marks=marks,
        maneuvers=[], loa_m=10.0,
    )
    assert out is not None
    assert out["overstand_m"] == 0.0


def test_understand_counts_tacks_near_mark():
    marks = [{"lat": 42.0, "lon": -87.6}, {"lat": 42.0001, "lon": -87.6}]
    pts = [_pt(s, lat=42.0, lon=-87.6, cog=315.0) for s in range(60)]
    tacks = [
        Maneuver(kind="tack", t=T0 + timedelta(seconds=30), leg_n=1,
                 from_tack="port", cog_delta_deg=90.0,
                 loss_boatlengths=1.0, loss_kt_s=20.0),
    ]
    out = analyze_laylines(
        _upwind_leg(pts), polar=_FakePolar(), marks=marks,
        maneuvers=tacks, loa_m=10.0,
    )
    assert out is not None
    assert out["understand_tacks_near_mark"] == 1


# ─── roundings (1.6) ──────────────────────────────────────────────────


def test_rounding_loss():
    ts = T0 + timedelta(seconds=300)
    pts = (
        [_pt(s, sog=6.0) for s in range(0, 300)]
        + [_pt(s, sog=4.0) for s in range(300, 600)]
    )
    out = analyze_roundings(
        pts,
        marks=[{"lat": 42.0, "lon": -87.6}, {"lat": 42.0, "lon": -87.6},
               {"lat": 42.1, "lon": -87.6}],
        mark_passes=[
            {"mark_index": 0, "ts": T0.isoformat()},
            {"mark_index": 1, "ts": ts.isoformat()},
        ],
        loa_m=10.0,
    )
    # Start pass (index 0) skipped; mark 1 analysed.
    assert len(out) == 1
    r = out[0]
    assert r["mark_index"] == 1
    assert abs(r["sog_in_kts"] - 6.0) < 0.2
    assert abs(r["sog_out_kts"] - 4.0) < 0.2
    assert 0.28 <= r["rounding_loss_pct"] <= 0.38
    # Boat is parked on the mark in this fixture → near-mark time high.
    assert r["time_within_3bl_s"] > 60


def test_finish_pass_has_no_out_speed():
    ts = T0 + timedelta(seconds=300)
    pts = [_pt(s) for s in range(0, 400)]
    out = analyze_roundings(
        pts,
        marks=[{"lat": 42.0, "lon": -87.6}, {"lat": 42.0, "lon": -87.6}],
        mark_passes=[
            {"mark_index": 0, "ts": T0.isoformat()},
            {"mark_index": 1, "ts": ts.isoformat()},
        ],
        loa_m=10.0,
    )
    assert len(out) == 1
    assert out[0]["is_finish"] is True
    assert out[0]["sog_out_kts"] is None
    assert out[0]["rounding_loss_pct"] is None


# ─── leeway / current (1.7) ───────────────────────────────────────────


def _imu(s, yaw):
    return {"recorded_at": T0 + timedelta(seconds=s), "yaw_deg": yaw}


def test_leeway_decomposition_flips_with_tack():
    """COG−yaw drift of +5° on starboard and −5° on port = pure leeway,
    no current."""
    pts, imu = [], []
    for s in range(120):   # port tack: COG 45, heading 50 → drift −5
        pts.append(_pt(s, cog=45.0))
        imu.append(_imu(s, 50.0))
    for s in range(120, 240):  # starboard: COG 315, heading 310 → +5
        pts.append(_pt(s, cog=315.0))
        imu.append(_imu(s, 310.0))
    out = analyze_leeway(pts, imu)
    assert out is not None
    assert abs(out["mean_drift_port_deg"] - (-5.0)) < 1.0
    assert abs(out["mean_drift_starboard_deg"] - 5.0) < 1.0
    assert abs(out["leeway_deg"] - 5.0) < 1.0
    assert "current_inference" not in out


def test_current_detected_when_drift_sign_stable():
    """Drift +8° on BOTH tacks = current, not leeway."""
    pts, imu = [], []
    for s in range(120):
        pts.append(_pt(s, cog=45.0))
        imu.append(_imu(s, 37.0))    # drift +8
    for s in range(120, 240):
        pts.append(_pt(s, cog=315.0))
        imu.append(_imu(s, 307.0))   # drift +8
    out = analyze_leeway(pts, imu)
    assert out is not None
    ci = out.get("current_inference")
    assert ci is not None
    assert abs(ci["cross_tack_component_deg"] - 8.0) < 1.0
    assert ci["estimated_drift_kts"] is not None
    assert 0.5 <= ci["estimated_drift_kts"] <= 1.2  # 6 kt × sin 8° ≈ 0.83
    assert ci["confidence"] in ("low", "med", "high")


def test_leeway_requires_imu_coverage():
    pts = [_pt(s, cog=45.0) for s in range(240)]
    assert analyze_leeway(pts, []) is None
    # Sparse IMU (one sample) → below per-tack minimum.
    assert analyze_leeway(pts, [_imu(0, 40.0)]) is None


# ─── wind timeline (1.8) ──────────────────────────────────────────────


def test_timeline_rows_and_persistent_shift():
    def wind_at(lat, lon, t):
        # 15° right shift 30 min in, never returns.
        mins = (t - T0).total_seconds() / 60.0
        return (0.0 if mins < 30 else 15.0), 10.0

    pts = [_pt(s) for s in range(0, 3600, 5)]
    rows, events = build_wind_timeline(
        pts, wind_at=wind_at, obs_snapshot=None, source="model",
        t_start=T0, t_end=T0 + timedelta(hours=1),
    )
    assert len(rows) >= 10          # 5-min cadence over an hour
    shift_events = [e for e in events if e["type"] == "persistent_shift"]
    assert len(shift_events) == 1
    assert shift_events[0]["direction"] == "right"
    assert abs(shift_events[0]["magnitude_deg"] - 15.0) < 1.0


def test_timeline_oscillation_returns():
    def wind_at(lat, lon, t):
        mins = (t - T0).total_seconds() / 60.0
        # 12° excursion for one 5-min row, then back.
        return (12.0 if 30 <= mins < 35 else 0.0), 10.0

    pts = [_pt(s) for s in range(0, 3600, 5)]
    _, events = build_wind_timeline(
        pts, wind_at=wind_at, obs_snapshot=None, source="model",
        t_start=T0, t_end=T0 + timedelta(hours=1),
    )
    osc = [e for e in events if e["type"] == "oscillation"]
    assert len(osc) >= 1


def test_timeline_building_trend():
    def wind_at(lat, lon, t):
        hrs = (t - T0).total_seconds() / 3600.0
        return 0.0, 8.0 + 6.0 * hrs   # 8 → 14 kt over the hour

    pts = [_pt(s) for s in range(0, 3600, 5)]
    rows, events = build_wind_timeline(
        pts, wind_at=wind_at, obs_snapshot=None, source="model",
        t_start=T0, t_end=T0 + timedelta(hours=1),
    )
    assert any(e["type"] == "building" for e in events)
    sig = summarize_conditions(rows, events)
    assert sig is not None
    assert sig["tws_trend"] == "building"
    assert sig["tws_lo_kts"] >= 7.5 and sig["tws_hi_kts"] <= 14.5


def test_timeline_gust_from_obs():
    obs = {
        "stations": [{
            "distance_km": 5.0,
            "obs": [
                {"ts": (T0 + timedelta(minutes=m)).isoformat(),
                 "gst_mps": 7.7}   # ~15 kt
                for m in range(0, 60, 10)
            ],
        }],
    }
    pts = [_pt(s) for s in range(0, 3600, 5)]
    rows, _ = build_wind_timeline(
        pts, wind_at=lambda la, lo, t: (0.0, 10.0), obs_snapshot=obs,
        source="blended", t_start=T0, t_end=T0 + timedelta(hours=1),
    )
    gusts = [r["gust_kts"] for r in rows if r["gust_kts"] is not None]
    assert gusts and abs(gusts[0] - 15.0) < 0.5


def test_forecast_rows_shape():
    class _F:
        def sample(self, lat, lon, t):
            return 0.0, -5.144  # 10 kt northerly

    rows = forecast_rows(
        _F(), lat=42.0, lon=-87.6,
        t_start=T0, t_end=T0 + timedelta(hours=1), interval_s=300,
    )
    assert len(rows) == 13
    assert rows[0]["source"] == "forecast"
    assert abs(rows[0]["tws_kts"] - 10.0) < 0.1
    assert detect_events(rows) == []  # steady wind → no events


# ─── tactician call replay (1.9) ──────────────────────────────────────


def _call(s, ctype, msg="msg"):
    return {
        "created_at": T0 + timedelta(seconds=s),
        "call_type": ctype, "message": msg, "eta": None,
    }


def test_layline_call_compliance():
    pts = [_pt(s) for s in range(600)]
    tack = Maneuver(kind="tack", t=T0 + timedelta(seconds=160), leg_n=1,
                    from_tack="port", cog_delta_deg=90.0,
                    loss_boatlengths=1.0, loss_kt_s=20.0)
    out = replay_calls(
        [_call(100, "layline"), _call(400, "layline")],
        points=pts, maneuvers=[tack],
    )
    assert out is not None
    recs = out["calls"]
    assert recs[0]["responded"] is True    # tack 60 s after the call
    assert recs[1]["responded"] is False   # no maneuver in window
    comp = out["compliance_by_type"]["layline"]
    assert comp["count"] == 2
    assert comp["compliance"] == 0.5


def test_off_pace_call_speed_recovery():
    pts = (
        [_pt(s, sog=4.0) for s in range(0, 130)]
        + [_pt(s, sog=5.5) for s in range(130, 400)]
    )
    out = replay_calls([_call(120, "off_pace")], points=pts, maneuvers=[])
    assert out["calls"][0]["responded"] is True


def test_over_heel_not_assessable():
    pts = [_pt(s) for s in range(300)]
    out = replay_calls([_call(100, "over_heel")], points=pts, maneuvers=[])
    assert out["calls"][0]["responded"] is None
    assert out["compliance_by_type"]["over_heel"]["compliance"] is None


def test_replay_returns_none_without_calls():
    assert replay_calls([], points=[_pt(0)], maneuvers=[]) is None


# ─── condition signature ──────────────────────────────────────────────


_SIG = {
    "tws_lo_kts": 8.0, "tws_hi_kts": 12.0, "twd_mean_deg": 220.0,
    "character": "oscillating", "osc_amplitude_deg": 12.0,
    "tws_trend": "steady",
}


def test_signature_text_form():
    txt = signature_text(_SIG)
    assert "TWS 8-12 kt" in txt
    assert "220" in txt
    assert "oscillating" in txt


def test_match_score_identical_is_high():
    assert match_score(_SIG, dict(_SIG)) > 0.95


def test_match_score_disjoint_tws_is_low():
    other = {**_SIG, "tws_lo_kts": 22.0, "tws_hi_kts": 28.0}
    assert match_score(_SIG, other) < MATCH_THRESHOLD


def test_match_score_handles_missing():
    assert match_score(None, _SIG) == 0.0
    assert match_score(_SIG, {}) < MATCH_THRESHOLD


def test_best_match_applies_threshold():
    good = {"signature": dict(_SIG), "directives": ["a"], "race_name": "R1"}
    bad = {
        "signature": {**_SIG, "tws_lo_kts": 25.0, "tws_hi_kts": 30.0,
                      "character": "steady", "twd_mean_deg": 40.0},
        "directives": ["b"], "race_name": "R2",
    }
    win = best_match(_SIG, [bad, good])
    assert win is not None and win["race_name"] == "R1"
    assert win["score"] >= MATCH_THRESHOLD
    assert best_match(_SIG, [bad]) is None
