"""Tests for app/services/mark_gates.py (v4 gate detection) and
app/services/start_line.py (bearing math), plus the track_ingest
pre-start filter — the fixes born from the 2026-07-01 Beer Can
7.1.2026 race, where a normal start-line crossing ~390 m from the
committee mark never registered under the v3 point+radius model.

Pure-function style matches test_mark_rounding.py: synthetic points
around Lake Michigan latitudes, no DB (track_ingest tests use the
same MagicMock-conn pattern as test_track_ingest.py).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services import track_ingest
from app.services.mark_gates import (
    GateAwareDetector,
    GateSpec,
    LINE_HALF_LEN_M,
    RAY_LEN_M,
    build_gates,
)
from app.services.mark_rounding import (
    Mark,
    Point,
    thresholds_for_course,
)
from app.services.start_line import line_bearing_from_wind, wind_from_deg


REF_LAT = 42.0
REF_LON = -87.6
BASE_TS = datetime(2026, 7, 2, 0, 15, tzinfo=timezone.utc)


def m_to_dlat(m: float) -> float:
    return m / 111_000.0


def m_to_dlon(m: float, at_lat: float = REF_LAT) -> float:
    return m / (111_000.0 * math.cos(math.radians(at_lat)))


def pt(lat: float, lon: float, t_offset_s: float) -> Point:
    return Point(lat=lat, lon=lon, ts=BASE_TS + timedelta(seconds=t_offset_s))


# ─── Bearing math (start_line.py) ─────────────────────────────────────


def test_wind_from_deg_cardinal_directions():
    # Northerly (wind FROM the north blows toward the south): v < 0.
    assert wind_from_deg(0.0, -5.0) == pytest.approx(0.0)
    # Easterly: blows toward the west, u < 0.
    assert wind_from_deg(-5.0, 0.0) == pytest.approx(90.0)
    # Southerly: v > 0.
    assert wind_from_deg(0.0, 5.0) == pytest.approx(180.0)
    # Westerly: u > 0.
    assert wind_from_deg(5.0, 0.0) == pytest.approx(270.0)


def test_line_bearing_perpendicular_to_wind():
    # The user's example: wind from the north → line runs east/west.
    assert line_bearing_from_wind(0.0, -5.0) == pytest.approx(90.0)
    # Wind from the west → line runs north/south (0° == 360°).
    assert line_bearing_from_wind(5.0, 0.0) == pytest.approx(0.0)


# ─── build_gates ──────────────────────────────────────────────────────


def _course_marks() -> list[dict]:
    """Start, one rounding mark north, finish back at the start pos."""
    return [
        {"name": "SA7", "lat": REF_LAT, "lon": REF_LON},
        {
            "name": "M1",
            "lat": REF_LAT + m_to_dlat(2000),
            "lon": REF_LON,
            "rounding": "starboard",
        },
        {"name": "Finish", "lat": REF_LAT, "lon": REF_LON, "rounding": "port"},
    ]


def test_build_gates_kinds_with_bearing():
    gates = build_gates(_course_marks(), line_bearing_deg=90.0)
    assert [g.kind for g in gates] == ["line", "ray", "line"]
    # Start gate is direction-gated toward mark 1 (destination side).
    assert gates[0].ref is not None and gates[0].ref_is_origin is False
    # Finish gate is direction-gated from the previous mark (origin side).
    assert gates[2].ref is not None and gates[2].ref_is_origin is True


def test_build_gates_falls_back_to_cpa_without_bearing_or_rounding():
    """No bearing: start degrades to CPA (i == 0 can't build a ray);
    a rounding-less intermediate mark degrades to CPA; but a FINISH
    with a rounding side keeps a ray — ray+CPA union detects strictly
    more than CPA alone, and under-detection at the last mark is what
    wedged the 7.1 Beer Can. Decision 2026-07-02."""
    marks = _course_marks()
    del marks[1]["rounding"]
    gates = build_gates(marks, line_bearing_deg=None)
    assert [g.kind for g in gates] == ["cpa", "cpa", "ray"]


def test_build_gates_all_cpa_without_bearing_or_any_rounding():
    """With no bearing and no rounding values anywhere, everything
    degrades to v3 CPA."""
    marks = _course_marks()
    del marks[1]["rounding"]
    del marks[2]["rounding"]
    gates = build_gates(marks, line_bearing_deg=None)
    assert [g.kind for g in gates] == ["cpa", "cpa", "cpa"]


def test_build_gates_ray_points_to_passing_side():
    """Inbound leg heads north (0°). Leave to starboard → the mark is on
    the boat's right → the boat passes on the mark's WEST side → the ray
    must extend west (negative longitude offset)."""
    gates = build_gates(_course_marks(), line_bearing_deg=90.0)
    ray = gates[1]
    assert ray.kind == "ray"
    assert ray.b is not None
    assert ray.b[1] < ray.mark_lon  # west
    # Ray length ≈ RAY_LEN_M.
    dlon_m = abs(ray.b[1] - ray.mark_lon) * 111_000.0 * math.cos(
        math.radians(ray.mark_lat)
    )
    assert dlon_m == pytest.approx(RAY_LEN_M, rel=0.01)


# ─── GateSpec.crossing / GateAwareDetector ────────────────────────────


def _detector(marks: list[dict], bearing, next_idx: int = 0, state=None):
    det_marks = [Mark(lat=m["lat"], lon=m["lon"]) for m in marks]
    return GateAwareDetector(
        det_marks,
        gates=build_gates(marks, bearing),
        threshold_m=thresholds_for_course(len(marks), mode="inshore"),
        next_mark_index=next_idx,
        state=state,
    )


def test_start_line_crossing_far_from_mark_detected():
    """THE Beer Can 7.1.2026 regression: a legitimate start crossing
    ~400 m from the committee mark. v3 (100 m inshore radius) missed
    it; the v4 line gate must not."""
    marks = _course_marks()
    det = _detector(marks, bearing=90.0)  # east-west line through SA7
    x = m_to_dlon(400)  # 400 m east of the mark
    passes = det.feed_batch([
        pt(REF_LAT - m_to_dlat(150), REF_LON + x, 0),   # south of line
        pt(REF_LAT + m_to_dlat(150), REF_LON + x, 10),  # north (toward M1)
    ])
    assert len(passes) == 1
    assert passes[0].mark_index == 0
    # Interpolated crossing sits on the line (≈ mark latitude) at the
    # 400 m-east longitude, timestamped mid-segment.
    assert passes[0].lat == pytest.approx(REF_LAT, abs=1e-4)
    assert passes[0].lon == pytest.approx(REF_LON + x, abs=1e-5)
    assert BASE_TS < passes[0].ts < BASE_TS + timedelta(seconds=10)


def test_start_line_wrong_direction_not_detected():
    """Sailing back over the line (away from leg 1) must not fire."""
    marks = _course_marks()
    det = _detector(marks, bearing=90.0)
    x = m_to_dlon(400)
    passes = det.feed_batch([
        pt(REF_LAT + m_to_dlat(150), REF_LON + x, 0),   # north
        pt(REF_LAT - m_to_dlat(150), REF_LON + x, 10),  # south — wrong way
    ])
    assert passes == []


def test_start_line_crossing_beyond_line_length_not_detected():
    marks = _course_marks()
    det = _detector(marks, bearing=90.0)
    x = m_to_dlon(LINE_HALF_LEN_M + 500)  # past the pin end
    passes = det.feed_batch([
        pt(REF_LAT - m_to_dlat(150), REF_LON + x, 0),
        pt(REF_LAT + m_to_dlat(150), REF_LON + x, 10),
    ])
    assert passes == []


def test_ray_rounding_wide_pass_detected():
    """Passage-mark case: 300 m off the mark on the correct side — the
    2026-06-26 Silly Race failure mode (342 m CPA missed). No radius to
    be outside of; crossing the abeam ray counts."""
    marks = _course_marks()
    det = _detector(marks, bearing=90.0, next_idx=1)  # start already passed
    m1 = marks[1]
    x = m_to_dlon(300)
    passes = det.feed_batch([
        pt(m1["lat"] - m_to_dlat(200), m1["lon"] - x, 0),   # south, west side
        pt(m1["lat"] + m_to_dlat(200), m1["lon"] - x, 20),  # north, west side
    ])
    assert [p.mark_index for p in passes] == [1]


def test_ray_rounding_wrong_side_not_detected():
    """Same wide pass on the WRONG side (east, when leave-to-starboard
    means the boat belongs west) — no gate crossing, and 300 m is
    outside the inshore CPA threshold, so nothing fires."""
    marks = _course_marks()
    det = _detector(marks, bearing=90.0, next_idx=1)
    m1 = marks[1]
    x = m_to_dlon(300)
    passes = det.feed_batch([
        pt(m1["lat"] - m_to_dlat(200), m1["lon"] + x, 0),
        pt(m1["lat"] + m_to_dlat(200), m1["lon"] + x, 20),
    ])
    assert passes == []


def test_gate_crossing_across_batch_boundary():
    """The crossing segment's two samples arrive in different batches —
    dump_state/restore must carry the previous sample so the segment
    still completes (mobile uploads batches of 1-3 samples)."""
    marks = _course_marks()
    det1 = _detector(marks, bearing=90.0)
    x = m_to_dlon(400)
    assert det1.feed_batch([pt(REF_LAT - m_to_dlat(150), REF_LON + x, 0)]) == []
    state = det1.dump_state()
    assert state is not None and "prev_lat" in state
    # JSONB round-trip, then resume in a fresh detector.
    state = json.loads(json.dumps(state))
    det2 = _detector(marks, bearing=90.0, state=state)
    passes = det2.feed_batch([pt(REF_LAT + m_to_dlat(150), REF_LON + x, 10)])
    assert [p.mark_index for p in passes] == [0]


def test_cpa_fallback_still_works_without_gates():
    """No bearing, no rounding sides → pure v3 behaviour: a tight pass
    with clear departure still emits."""
    marks = [
        {"name": "A", "lat": REF_LAT, "lon": REF_LON},
        {"name": "B", "lat": REF_LAT + m_to_dlat(2000), "lon": REF_LON},
    ]
    det = _detector(marks, bearing=None)
    # March east straight over mark A: approach, CPA ~0, depart 3+.
    pts = [
        pt(REF_LAT, REF_LON + m_to_dlon(dx), i * 5)
        for i, dx in enumerate([-200, -100, 0, 60, 120, 180, 240])
    ]
    passes = det.feed_batch(pts)
    assert [p.mark_index for p in passes] == [0]


# ─── track_ingest integration (pre-start filter + line start) ─────────


@pytest.fixture
def conn() -> MagicMock:
    c = MagicMock()
    c.fetchrow = AsyncMock()
    c.execute = AsyncMock()
    return c


def _beer_can_marks() -> list[dict]:
    # Geometry echoing Beer Can 7.1.2026: start SA7, first leg to SW.
    return [
        {"name": "SA7", "lat": 41.852833, "lon": -87.556833},
        {"name": "6", "lat": 41.84, "lon": -87.574, "rounding": "port"},
    ]


async def test_pre_start_points_and_poisoned_state_ignored(conn):
    """Regression for the 2026-07-01 failure chain: pre-gun samples are
    dropped, a persisted pre-gun detector_state is discarded, and the
    post-gun line crossing registers the start."""
    gun = datetime(2026, 7, 2, 0, 15, tzinfo=timezone.utc)
    marks = _beer_can_marks()
    lat0, lon0 = marks[0]["lat"], marks[0]["lon"]
    dlat = 150 / 111_000.0
    x = 400 / (111_000.0 * math.cos(math.radians(lat0)))  # 400 m east

    poisoned = {
        "min_ts": "2026-07-02T00:13:27.853000+00:00",  # pre-gun!
        "min_lat": 41.8558725,
        "min_lon": -87.559178,
        "min_dist": 389.76,
        "departing": 0,
        "last_dist": 389.76,
    }
    points = [
        # Pre-gun milling around near the mark — must be ignored.
        track_ingest.DetectorPoint(
            lat=lat0 + dlat, lon=lon0, ts=gun - timedelta(seconds=60)
        ),
        # Post-gun: crossing the east-west line 400 m east of the mark,
        # heading south toward mark "6".
        track_ingest.DetectorPoint(
            lat=lat0 + dlat, lon=lon0 + x, ts=gun + timedelta(seconds=20)
        ),
        track_ingest.DetectorPoint(
            lat=lat0 - dlat, lon=lon0 + x, ts=gun + timedelta(seconds=40)
        ),
    ]

    all_p, new_p = await track_ingest.detect_and_persist_new_passes(
        conn,
        race_id=uuid4(),
        marks=marks,
        existing_passes=[],
        new_points=points,
        started_at=gun,
        start_at=gun,
        mode="inshore",
        detector_state=poisoned,
        start_line_bearing_override=90.0,  # east-west line; skips forecast
    )

    assert [p["mark_index"] for p in new_p] == [0]
    assert all_p == new_p
    # The pass UPDATE persisted mark_passes.
    update_sql = conn.execute.await_args.args[0]
    assert "mark_passes" in update_sql


async def test_pre_start_filter_cpa_path(conn):
    """Same filter protects the CPA fallback: with no line bearing, a
    poisoned pre-gun state is discarded and a clean post-gun rounding
    (tight pass + departure) still emits."""
    gun = datetime(2026, 7, 2, 0, 15, tzinfo=timezone.utc)
    marks = [
        {"name": "SA7", "lat": 41.852833, "lon": -87.556833},
        {"name": "6", "lat": 41.84, "lon": -87.574},
    ]
    lat0, lon0 = marks[0]["lat"], marks[0]["lon"]

    def east(m: float) -> float:
        return m / (111_000.0 * math.cos(math.radians(lat0)))

    poisoned = {
        "min_ts": "2026-07-02T00:13:27.853000+00:00",
        "min_lat": 41.8558725,
        "min_lon": -87.559178,
        "min_dist": 389.76,
        "departing": 0,
        "last_dist": 389.76,
    }
    offsets = [-200, -80, 0, 60, 120, 180, 240]  # metres east of mark
    points = [
        track_ingest.DetectorPoint(
            lat=lat0, lon=lon0 + east(dx), ts=gun + timedelta(seconds=10 + i * 5)
        )
        for i, dx in enumerate(offsets)
    ]

    _, new_p = await track_ingest.detect_and_persist_new_passes(
        conn,
        race_id=uuid4(),
        marks=marks,
        existing_passes=[],
        new_points=points,
        started_at=gun,
        start_at=gun,
        mode="inshore",
        detector_state=poisoned,
    )

    assert [p["mark_index"] for p in new_p] == [0]
    # The emitted CPA is the post-gun 0 m point, not the poisoned 390 m
    # pre-gun one.
    assert new_p[0]["ts"] >= gun.isoformat()
