"""Tests for the in-race tactician's pure layer.

Covers ``services/tactics/``: detectors (synthetic tracks/winds),
sustained-heel + mount-quality gate, heel-band lookup, advisor pure
helpers (SILENT contract, truncation, fake-client round trip),
snapshot shape, and the new Redis key fingerprints.

Pipeline orchestration (cooldowns via Redis, DB loads, publish) is
exercised on deploy and flagged for a dedicated mocked-I/O test pass —
see the 2026-06-11 session summary's tech-debt list.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.tactics import advisor
from app.services.tactics.detectors import (
    ANNOUNCE_MAX_LEAD_S,
    ANNOUNCE_MIN_LEAD_S,
    CallCandidate,
    detect_forecast_shift,
    detect_layline,
    detect_off_pace,
    detect_over_heel,
    detect_pinching,
    detect_plan_divergence,
    detect_planned_maneuver,
    run_detectors,
    target_twa,
)
from app.services.tactics.heel import (
    MAX_PLAUSIBLE_STDEV_DEG,
    MIN_SAMPLES,
    sustained_heel,
)
from app.services.tactics.heel_bands import GENERIC_BANDS, band_for
from app.services.tactics.snapshot import build_snapshot
from app.services.redis_keys import (
    route_current_key,
    tactics_cooldown_key,
    tactics_latest_key,
)

NOW = datetime(2026, 6, 11, 18, 0, 0, tzinfo=timezone.utc)

# Roughly Chicago's Monroe Harbor.
LAT0, LON0 = 41.88, -87.60

# Metres per degree at LAT0.
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))


class FakePolar:
    """Piecewise polar: 4 kt below TWA 45, 6 kt at/above.

    target_twa scans TWA from 30° in 2° steps (30, 32, …, 44, 46, …) —
    45 itself is never sampled, so the best sampled upwind VMG lands at
    46° (6·cos46 ≈ 4.17, beating 44° at 4·cos44 ≈ 2.88 and 48° at
    6·cos48 ≈ 4.01).
    """

    def boat_speed(self, twa: float, tws: float) -> float:
        return 6.0 if twa >= 45.0 else 4.0


class FakeForecast:
    """Wind-from direction (deg) and speed (kt) as functions of time."""

    def __init__(self, twd_fn, tws_kt: float = 12.0):
        self._twd_fn = twd_fn
        self._tws = tws_kt

    def sample(self, lat, lon, t):
        twd = self._twd_fn(t)
        if twd is None:
            return None
        to_rad = math.radians((twd + 180.0) % 360.0)
        ms = self._tws * 0.514444
        return (ms * math.sin(to_rad), ms * math.cos(to_rad))


def steady_wind(twd: float, tws_kt: float = 12.0) -> FakeForecast:
    return FakeForecast(lambda t: twd, tws_kt)


def mk_track(
    *,
    n: int = 30,
    dt_s: float = 5.0,
    cog: float = 45.0,
    sog: float = 6.0,
    end: datetime = NOW,
    lat: float = LAT0,
    lon: float = LON0,
) -> list[dict]:
    """Straight-line track ending at ``end`` at (lat, lon)."""
    out = []
    for i in range(n):
        t = end - timedelta(seconds=dt_s * (n - 1 - i))
        out.append({"t": t, "lat": lat, "lon": lon,
                    "sog_kts": sog, "cog_deg": cog})
    return out


def mk_evals(
    *,
    n: int = 12,
    dt_s: float = 5.0,
    end: datetime = NOW,
    twa: float = 45.0,
    tws: float = 12.0,
    speed_ratio: float = 1.0,
    vmg_ratio: float = 1.0,
) -> list[dict]:
    out = []
    for i in range(n):
        t = end - timedelta(seconds=dt_s * (n - 1 - i))
        out.append({
            "t": t, "twa": twa, "tws_kts": tws, "twd": 0.0,
            "target_kts": 6.0, "actual_kts": 6.0 * speed_ratio,
            "speed_ratio": speed_ratio, "target_vmg": 4.2,
            "actual_vmg": 4.2 * vmg_ratio, "vmg_ratio": vmg_ratio,
        })
    return out


# ─── target_twa ──────────────────────────────────────────────────────────


def test_target_twa_finds_best_vmg_angle():
    # 46, not 45 — see FakePolar docstring (2° scan grid from 30°).
    assert target_twa(FakePolar(), 12.0, upwind=True) == 46.0


def test_target_twa_none_when_polar_dead():
    class DeadPolar:
        def boat_speed(self, twa, tws):
            return 0.0
    assert target_twa(DeadPolar(), 12.0, upwind=True) is None


# ─── planned maneuver ────────────────────────────────────────────────────


def _route_north_then_east(turn_after_m: float) -> list[tuple[float, float]]:
    """LineString from the boat's position: due north, then 90° right."""
    coords = [(LON0, LAT0)]
    step_m = 150.0
    steps = int(turn_after_m / step_m)
    lat = LAT0
    for _ in range(steps):
        lat += step_m / M_PER_DEG_LAT
        coords.append((LON0, lat))
    # The eastward leg after the turn.
    lon = LON0
    for _ in range(4):
        lon += step_m / M_PER_DEG_LON
        coords.append((lon, lat))
    return coords


def test_planned_maneuver_announces_turn_in_window():
    # 6 kt ≈ 3.09 m/s ⇒ 600 m ≈ 194 s — inside [90, 300].
    track = mk_track(cog=0.0, sog=6.0)
    cand = detect_planned_maneuver(track, _route_north_then_east(600.0), NOW)
    assert cand is not None
    assert cand.call_type == "planned_maneuver"
    assert cand.call_class == "maneuver"
    assert cand.diagnosis["direction"] == "starboard"
    eta_s = (cand.eta - NOW).total_seconds()
    assert ANNOUNCE_MIN_LEAD_S <= eta_s <= ANNOUNCE_MAX_LEAD_S


def test_planned_maneuver_quiet_when_turn_too_far():
    # 2.5 km at 6 kt ≈ 810 s — beyond the window.
    track = mk_track(cog=0.0, sog=6.0)
    assert detect_planned_maneuver(
        track, _route_north_then_east(2500.0), NOW) is None


def test_planned_maneuver_quiet_when_turn_too_close():
    # 150 m ≈ 49 s — inside min lead; calling now would be late.
    track = mk_track(cog=0.0, sog=6.0)
    assert detect_planned_maneuver(
        track, _route_north_then_east(150.0), NOW) is None


def test_planned_maneuver_quiet_when_off_plan():
    track = mk_track(cog=0.0, sog=6.0, lon=LON0 + 0.02)  # ~1.6 km off
    assert detect_planned_maneuver(
        track, _route_north_then_east(600.0), NOW) is None


def test_planned_maneuver_quiet_without_route():
    assert detect_planned_maneuver(mk_track(), None, NOW) is None


# ─── layline ─────────────────────────────────────────────────────────────


def test_layline_announces_in_window():
    # Wind from north; boat close-hauled port tack (cog 45). The
    # starboard-tack layline (course 315) through a mark up and to the
    # right gets closed at ~half boat speed.
    track = mk_track(cog=45.0, sog=6.0)
    mark = {"lat": LAT0 + 900.0 / M_PER_DEG_LAT, "lon": LON0,
            "label": "Windward"}
    cand = detect_layline(track, mark, steady_wind(0.0), FakePolar(), NOW)
    assert cand is not None
    assert cand.call_type == "layline"
    assert cand.diagnosis["tack"] == "port"
    assert cand.diagnosis["layline_for"] == "starboard"
    eta_s = (cand.eta - NOW).total_seconds()
    assert ANNOUNCE_MIN_LEAD_S <= eta_s <= ANNOUNCE_MAX_LEAD_S


def test_layline_quiet_off_the_wind():
    # TWA 120 — running; laylines don't apply.
    track = mk_track(cog=120.0, sog=6.0)
    mark = {"lat": LAT0 + 0.01, "lon": LON0}
    assert detect_layline(
        track, mark, steady_wind(0.0), FakePolar(), NOW) is None


def test_layline_quiet_without_mark_or_wind():
    track = mk_track(cog=45.0)
    assert detect_layline(track, None, steady_wind(0.0), FakePolar(), NOW) is None
    mark = {"lat": LAT0 + 0.01, "lon": LON0}
    assert detect_layline(
        track, mark, FakeForecast(lambda t: None), FakePolar(), NOW) is None


# ─── forecast shift ──────────────────────────────────────────────────────


def test_forecast_shift_fires_on_future_veer():
    def twd_fn(t):
        return 20.0 if (t - NOW).total_seconds() >= 600 else 0.0
    cand = detect_forecast_shift(mk_track(), FakeForecast(twd_fn), NOW)
    assert cand is not None
    assert cand.call_type == "forecast_shift"
    assert cand.diagnosis["shift_direction"] == "right"
    assert cand.diagnosis["shift_deg"] == 20.0
    assert (cand.eta - NOW).total_seconds() == 600.0


def test_forecast_shift_quiet_on_steady_wind():
    assert detect_forecast_shift(mk_track(), steady_wind(0.0), NOW) is None


def test_forecast_shift_quiet_below_threshold():
    def twd_fn(t):
        return 8.0 if (t - NOW).total_seconds() >= 300 else 0.0
    assert detect_forecast_shift(mk_track(), FakeForecast(twd_fn), NOW) is None


# ─── pinching / sailing low ──────────────────────────────────────────────


def test_pinching_fires_below_target_angle():
    evals = mk_evals(twa=35.0)  # target 45 ⇒ 10° high
    cand = detect_pinching(evals, mk_track(cog=10.0), FakePolar(), NOW)
    assert cand is not None
    assert cand.diagnosis["mode"] == "pinching"
    assert cand.call_class == "coaching"
    assert any("foot off" in a for a in cand.adjustments)


def test_sailing_low_fires_with_poor_vmg():
    evals = mk_evals(twa=60.0, vmg_ratio=0.8)
    cand = detect_pinching(evals, mk_track(cog=60.0), FakePolar(), NOW)
    assert cand is not None
    assert cand.diagnosis["mode"] == "sailing_low"


def test_pinching_quiet_on_target():
    evals = mk_evals(twa=45.0)
    assert detect_pinching(evals, mk_track(), FakePolar(), NOW) is None


def test_pinching_suppressed_mid_tack():
    evals = mk_evals(twa=35.0)
    track = mk_track(cog=10.0)
    track[-1] = {**track[-1], "cog_deg": 100.0}  # 90° swing in window
    assert detect_pinching(evals, track, FakePolar(), NOW) is None


def test_pinching_quiet_downwind():
    evals = mk_evals(twa=140.0)
    assert detect_pinching(evals, mk_track(cog=140.0), FakePolar(), NOW) is None


# ─── over-heel + mount gate + bands ──────────────────────────────────────


def _imu(n: int, heel: float, jitter: float = 0.0, end: datetime = NOW):
    out = []
    for i in range(n):
        t = end - timedelta(seconds=0.5 * (n - 1 - i))
        h = heel + (jitter if i % 2 else -jitter)
        out.append({"recorded_at": t, "heel_deg": h})
    return out


def test_sustained_heel_applies_calibration():
    cal = [{"captured_at": NOW - timedelta(hours=1),
            "heel_zero_offset_deg": 3.0, "pitch_zero_offset_deg": 0.0}]
    stat = sustained_heel(_imu(60, 28.0, 2.0), calibrations=cal, now=NOW)
    assert stat is not None
    assert stat["median_heel_deg"] == 25.0
    assert stat["mount_ok"] is True


def test_sustained_heel_none_when_sparse():
    assert sustained_heel(_imu(MIN_SAMPLES - 1, 20.0), now=NOW) is None


def test_mount_gate_trips_on_garbage():
    stat = sustained_heel(_imu(60, 10.0, MAX_PLAUSIBLE_STDEV_DEG + 10.0),
                          now=NOW)
    assert stat is not None
    assert stat["mount_ok"] is False


def test_over_heel_fires_beyond_band():
    stat = {"median_heel_deg": 27.0, "median_abs_deg": 27.0,
            "stdev_deg": 4.0, "sample_count": 60, "mount_ok": True}
    evals = mk_evals(twa=40.0, tws=16.0)
    cand = detect_over_heel(stat, evals, "Beneteau First 36.7", NOW)
    assert cand is not None
    assert cand.call_type == "over_heel"
    assert cand.adjustments  # rule-table actions attached
    assert cand.diagnosis["band_hi_deg"] == 25.0


def test_over_heel_respects_mount_gate():
    stat = {"median_abs_deg": 40.0, "median_heel_deg": 40.0,
            "stdev_deg": 30.0, "sample_count": 60, "mount_ok": False}
    assert detect_over_heel(stat, mk_evals(), "Beneteau First 36.7", NOW) is None


def test_over_heel_quiet_inside_band():
    stat = {"median_heel_deg": 20.0, "median_abs_deg": 20.0,
            "stdev_deg": 4.0, "sample_count": 60, "mount_ok": True}
    assert detect_over_heel(stat, mk_evals(tws=16.0, twa=40.0),
                            "Beneteau First 36.7", NOW) is None


def test_band_lookup_falls_back_to_generic():
    band = band_for("J/105", 13.0, 40.0)
    assert band in GENERIC_BANDS
    assert band.upwind is True


# ─── off-pace + divergence ───────────────────────────────────────────────


def test_off_pace_fires_on_sustained_deficit():
    evals = mk_evals(n=25, speed_ratio=0.75)
    cand = detect_off_pace(evals, mk_track(cog=45.0), NOW)
    assert cand is not None
    assert cand.call_type == "off_pace"
    assert cand.diagnosis["mean_speed_ratio"] == 0.75


def test_off_pace_quiet_on_pace_and_mid_maneuver():
    assert detect_off_pace(mk_evals(n=25, speed_ratio=0.97),
                           mk_track(), NOW) is None
    # The COG swing must land INSIDE the detector's 90 s window.
    # mk_track(n=25, dt=5) spans 120 s ending at NOW, so index 12 sits
    # at NOW−60 s — well inside; index 0 (NOW−120 s) would be outside
    # and correctly ignored.
    track = mk_track(n=25)
    track[12] = {**track[12], "cog_deg": 150.0}
    assert detect_off_pace(mk_evals(n=25, speed_ratio=0.75), track, NOW) is None


def test_plan_divergence_fires_far_from_route():
    route = _route_north_then_east(1000.0)
    track = mk_track(lon=LON0 + 450.0 / M_PER_DEG_LON)
    cand = detect_plan_divergence(track, route)
    assert cand is not None
    assert cand.diagnosis["xte_m"] > 300


def test_plan_divergence_quiet_near_route():
    route = _route_north_then_east(1000.0)
    assert detect_plan_divergence(mk_track(), route) is None


# ─── runner priority + heel gate ─────────────────────────────────────────


def test_run_detectors_orders_by_priority_and_gates_heel():
    # Pinching + off-pace conditions both true; heel beyond band but
    # include_heel=False (the v1c gate).
    evals = mk_evals(n=25, twa=35.0, speed_ratio=0.75)
    heel_stat = {"median_heel_deg": 30.0, "median_abs_deg": 30.0,
                 "stdev_deg": 3.0, "sample_count": 60, "mount_ok": True}
    found = run_detectors(
        track=mk_track(n=25, cog=10.0), evals=evals,
        forecast=steady_wind(0.0), polar=FakePolar(),
        route_coords=None, next_mark=None, heel_stat=heel_stat,
        boat_class="Beneteau First 36.7", now=NOW, include_heel=False,
    )
    types = [c.call_type for c in found]
    assert "over_heel" not in types
    assert types == sorted(types, key=lambda t: {
        "pinching": 5, "off_pace": 6}.get(t, 99))
    assert types[0] == "pinching"

    found_heel = run_detectors(
        track=mk_track(n=25, cog=10.0), evals=evals,
        forecast=steady_wind(0.0), polar=FakePolar(),
        route_coords=None, next_mark=None, heel_stat=heel_stat,
        boat_class="Beneteau First 36.7", now=NOW, include_heel=True,
    )
    assert [c.call_type for c in found_heel][0] == "over_heel"


# ─── advisor pure helpers ────────────────────────────────────────────────


def test_parse_response_silent_and_empty():
    assert advisor.parse_response("SILENT") is None
    assert advisor.parse_response("  silent  ") is None
    assert advisor.parse_response("") is None
    assert advisor.parse_response(None) is None


def test_parse_response_strips_and_truncates():
    assert advisor.parse_response('  "Tack in about 2 minutes"  ') == \
        "Tack in about 2 minutes"
    long = "word " * 60
    out = advisor.parse_response(long)
    assert out is not None and len(out) <= advisor.MAX_CALL_CHARS
    assert not out.endswith(" ")


def test_generate_call_with_fake_client():
    fake_resp = SimpleNamespace(content=[
        SimpleNamespace(type="text", text="Tack in about 2 minutes at the layline"),
    ])
    calls = {}

    class FakeMessages:
        def create(self, **kwargs):
            calls.update(kwargs)
            return fake_resp

    client = SimpleNamespace(messages=FakeMessages())
    out = advisor.generate_call({"trigger": {"call_type": "layline"}},
                                client=client)
    assert out is not None
    assert out["message"].startswith("Tack in about 2 minutes")
    assert out["prompt_version"] == advisor.PROMPT_VERSION
    assert "layline" in calls["messages"][0]["content"]


def test_generate_call_silent_returns_none():
    fake_resp = SimpleNamespace(content=[
        SimpleNamespace(type="text", text="SILENT"),
    ])

    class FakeMessages:
        def create(self, **kwargs):
            return fake_resp

    assert advisor.generate_call(
        {}, client=SimpleNamespace(messages=FakeMessages())) is None


def test_generate_call_api_failure_returns_none():
    class FakeMessages:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    assert advisor.generate_call(
        {}, client=SimpleNamespace(messages=FakeMessages())) is None


# ─── snapshot ────────────────────────────────────────────────────────────


def test_snapshot_shape_maneuver():
    cand = CallCandidate(
        call_type="layline", call_class="maneuver",
        diagnosis={"eta_s": 120},
        eta=NOW + timedelta(seconds=120),
    )
    snap = build_snapshot(
        candidate=cand, other_candidates=[],
        race_meta={"race_name": "Beer Can", "boat_class": "Beneteau First 36.7",
                   "mode": "inshore", "leg_index": 1, "marks_total": 4},
        track=mk_track(), evals=mk_evals(), forecast=steady_wind(0.0),
        next_mark={"lat": LAT0 + 0.01, "lon": LON0, "label": "Mark 2"},
        heel_stat=None, recent_calls=[], now=NOW,
    )
    assert snap["trigger"]["seconds_until_event"] == 120
    assert snap["next_mark"]["distance_m"] > 0
    assert snap["wind_at_boat"][0]["in_min"] == 0
    assert snap["performance"]["samples_2min"] > 0
    assert "heel" not in snap


def test_snapshot_includes_adjustments_for_coaching():
    cand = CallCandidate(
        call_type="pinching", call_class="coaching",
        diagnosis={"mode": "pinching"},
        adjustments=("foot off — bear away about 8°",),
    )
    snap = build_snapshot(
        candidate=cand, other_candidates=[],
        race_meta={}, track=mk_track(), evals=mk_evals(),
        forecast=steady_wind(0.0), next_mark=None, heel_stat=None,
        recent_calls=[{"created_at": "2026-06-11T17:55:00",
                       "call_type": "off_pace", "message": "earlier call"}],
        now=NOW,
    )
    assert snap["trigger"]["candidate_adjustments"]
    assert "seconds_until_event" not in snap["trigger"]
    assert snap["recent_calls"][0]["message"] == "earlier call"


# ─── redis key fingerprints (same contract style as test_redis_keys) ─────


def test_tactics_key_fingerprints():
    rid = "0b6c9f4e-1111-2222-3333-444455556666"
    assert tactics_latest_key(rid) == f"tactics:latest:{rid}"
    assert tactics_cooldown_key(rid) == f"tactics:cooldown:{rid}"
    assert tactics_cooldown_key(rid, "layline") == \
        f"tactics:cooldown:{rid}:layline"
    assert route_current_key(rid) == f"route:current:{rid}"
