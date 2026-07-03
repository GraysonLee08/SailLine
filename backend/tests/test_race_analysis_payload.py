"""End-to-end test of race_analysis.build_race_analysis over a
synthetic two-leg race: beat north under an oscillating-ish northerly,
run back. Verifies section presence/omission and the token-budget
discipline (no raw point arrays in the payload).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.services.race_analysis import build_race_analysis

T0 = datetime(2026, 7, 1, 18, 0, 0, tzinfo=timezone.utc)


class _FakePolar:
    def boat_speed(self, twa_deg: float, tws_kts: float, **_) -> float:
        if twa_deg < 40.0:
            return 0.0
        return 6.0


def _snapshot(hours: float = 1.0) -> dict:
    """Constant 10 kt northerly over a box around the course."""
    steps = int(hours * 4) + 1
    times = [
        (T0 + timedelta(minutes=15 * i)).isoformat() for i in range(steps)
    ]
    return {
        "lats": [41.95, 42.05],
        "lons": [-87.65, -87.55],
        "times": times,
        "u_mps": [[[0.0, 0.0], [0.0, 0.0]] for _ in range(steps)],
        "v_mps": [[[-5.144, -5.144], [-5.144, -5.144]] for _ in range(steps)],
    }


def _track_rows() -> list[dict]:
    """1 Hz: 10 min beat (tacks every 2 min), 10 min run back."""
    rows = []
    lat = 42.0
    for s in range(0, 600):
        cog = 45.0 if (s // 120) % 2 == 0 else 315.0
        lat += 4.7e-5 * 0.7  # net northward progress
        rows.append({
            "recorded_at": T0 + timedelta(seconds=s),
            "lat": lat, "lon": -87.6,
            "speed_kts": 6.0, "heading_deg": cog, "gps_acc_m": 5.0,
        })
    for s in range(600, 1200):
        lat -= 4.7e-5
        rows.append({
            "recorded_at": T0 + timedelta(seconds=s),
            "lat": lat, "lon": -87.6,
            "speed_kts": 7.0, "heading_deg": 180.0, "gps_acc_m": 5.0,
        })
    return rows


_MARKS = [
    {"lat": 42.0, "lon": -87.6, "name": "Start"},
    {"lat": 42.0198, "lon": -87.6, "name": "W"},
    {"lat": 42.0, "lon": -87.6, "name": "Finish"},
]

_PASSES = [
    {"mark_index": 0, "ts": T0.isoformat(), "lat": 42.0, "lon": -87.6},
    {"mark_index": 1, "ts": (T0 + timedelta(seconds=600)).isoformat(),
     "lat": 42.0198, "lon": -87.6},
    {"mark_index": 2, "ts": (T0 + timedelta(seconds=1200)).isoformat(),
     "lat": 42.0, "lon": -87.6},
]


def _build(**overrides):
    kwargs = dict(
        track_rows=_track_rows(),
        imu_rows=[],
        marks=_MARKS,
        mark_passes=_PASSES,
        race_start_at=T0,
        mode="inshore",
        boat_class="J/70",
        loa_ft=22.8,
        ratings={"hcp": 111},
        polar=_FakePolar(),
        wind_snapshot=_snapshot(),
        obs_snapshot=None,
        performance_summary={
            "avg_speed_ratio": 0.94, "avg_vmg_efficiency": 0.91,
            "pct_time_on_target": 0.4,
            "by_leg": [
                {"leg_index": 1, "avg_speed_ratio": 0.93,
                 "avg_vmg_efficiency": 0.9},
                {"leg_index": 2, "avg_speed_ratio": 0.97,
                 "avg_vmg_efficiency": None},
            ],
        },
        tactician_call_rows=[],
        start_line_bearing_override=None,
        start_line_bearing_deg=90.0,
        stats={"elapsed_s": 1200.0, "corrected_time_s": 1100.0,
               "corrected_using": "hcp"},
    )
    kwargs.update(overrides)
    return build_race_analysis(**kwargs)


def test_payload_sections_present():
    p = _build()
    assert p is not None
    assert p["boat"]["class"] == "J/70"
    assert p["boat"]["loa_ft"] == 22.8
    assert p["boat"]["polar_summary"]["upwind_target_twa_deg"] == 40.0
    assert p["course"]["legs_count"] == 2
    # Start: line bearing given, gun at T0, wind present.
    assert "start" in p
    # Legs: beat then run, perf joined by leg number.
    legs = p["legs"]
    assert len(legs) == 2
    assert legs[0]["type"] == "upwind"
    assert legs[0]["speed_ratio"] == 0.93
    assert legs[0]["tacks"] >= 3           # tacked every 2 min for 10 min
    assert legs[1]["type"] == "run"
    # Shift block only on the upwind leg.
    assert "pct_time_lifted" in legs[0]
    assert "pct_time_lifted" not in legs[1]
    # Maneuver economy block.
    assert p["maneuvers"]["tacks"]["count"] >= 3
    # Conditions + signature (constant wind → steady character).
    assert p["condition_signature"]["character"] == "steady"
    assert "TWS" in p["condition_signature_text"]
    # Roundings: windward mark + finish (start skipped).
    assert len(p["mark_roundings"]) == 2
    # Result carries corrected time and overall perf ratios.
    assert p["result"]["corrected_s"] == 1100.0
    assert p["result"]["overall_speed_ratio"] == 0.94
    # No tactician calls → section omitted.
    assert "tactician_calls" not in p


def test_payload_omits_wind_sections_without_snapshot():
    p = _build(wind_snapshot=None)
    assert p is not None
    assert "conditions" not in p or "wind_timeline" not in p.get("conditions", {})
    assert "condition_signature" not in p
    # Legs still segment (geometry only), but without wind they can't
    # be classified.
    assert all(leg["type"] == "unknown" for leg in p["legs"])
    # Start omitted: no bearing derivable... unless the cached column
    # provided one — here it did (start_line_bearing_deg=90), but bias
    # needs wind. distance_to_line still reports.
    if "start" in p:
        assert "bias_deg" not in p["start"]


def test_payload_none_on_tiny_track():
    p = _build(track_rows=_track_rows()[:60])
    assert p is None


def test_payload_is_json_serialisable_and_compact():
    p = _build()
    blob = json.dumps(p, default=str)
    # Token-budget discipline: derived numbers only, no raw arrays.
    # ~20 min race must land far below the ~6K-token (~25 KB) target.
    assert len(blob) < 25_000


def test_payload_includes_call_replay_when_calls_exist():
    calls = [{
        "created_at": T0 + timedelta(seconds=180),
        "call_type": "layline", "message": "Tack in about 2 minutes",
        "eta": None,
    }]
    p = _build(tactician_call_rows=calls)
    assert p is not None
    tc = p["tactician_calls"]
    assert tc["compliance_by_type"]["layline"]["count"] == 1
    # The beat tacks every 120 s, so a maneuver lands within the window.
    assert tc["calls"][0]["responded"] is True
