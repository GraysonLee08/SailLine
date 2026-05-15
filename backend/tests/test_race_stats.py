"""Pure-function tests for app/services/race_stats.py.

No DB, no network. Track points are synthesised geometrically — same
pattern as test_mark_rounding.py. We work around Lake Michigan
latitudes (~42.05°N) so the haversine math matches production.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.services.race_stats import (
    DP_EPSILON_KT,
    GAP_THRESHOLD_S,
    LegSplit,
    MAX_SPEED_SAMPLES,
    MOVING_THRESHOLD_KT,
    RaceStats,
    SpeedSample,
    TrackPoint,
    _douglas_peucker,
    _haversine_m,
    _track_distance_in_window,
    compute_stats,
    pick_handicap,
    track_points_from_rows,
)


# ─── Geometry helpers ──────────────────────────────────────────────────

REF_LAT = 42.05
REF_LON = -87.75
BASE_T = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)


def m_to_dlat(m: float) -> float:
    return m / 111_000.0


def m_to_dlon(m: float, at_lat: float = REF_LAT) -> float:
    return m / (111_000.0 * math.cos(math.radians(at_lat)))


def tp(
    *,
    t_offset_s: float,
    east_m: float = 0.0,
    north_m: float = 0.0,
    speed_kts: float | None = None,
) -> TrackPoint:
    """One synthetic point at ``BASE_T + offset``, at ``REF`` shifted by
    east/north metres."""
    return TrackPoint(
        recorded_at=BASE_T + timedelta(seconds=t_offset_s),
        lat=REF_LAT + m_to_dlat(north_m),
        lon=REF_LON + m_to_dlon(east_m),
        speed_kts=speed_kts,
    )


def straight_line_track(
    *,
    n_points: int,
    spacing_m: float,
    dt_s: float,
    speed_kts: float | None = None,
) -> list[TrackPoint]:
    """``n_points`` points heading due east at constant speed."""
    return [
        tp(
            t_offset_s=i * dt_s,
            east_m=i * spacing_m,
            speed_kts=speed_kts,
        )
        for i in range(n_points)
    ]


# ─── Empty / single-point edge cases ──────────────────────────────────


def test_empty_track_returns_none():
    assert compute_stats([], marks=[], mark_passes=[]) is None


def test_single_point_returns_zero_stats():
    pts = [tp(t_offset_s=0)]
    stats = compute_stats(pts, marks=[], mark_passes=[])
    assert stats is not None
    assert stats.point_count == 1
    assert stats.elapsed_s == 0
    assert stats.moving_s == 0
    assert stats.stopped_s == 0
    assert stats.distance_m == 0
    assert stats.legs == []


# ─── Distance, time, SOG basics ───────────────────────────────────────


def test_straight_line_distance_matches_haversine():
    # 10 points, ~5m apart, 1s sampling. ~50 m straight east.
    pts = straight_line_track(
        n_points=10, spacing_m=50.0, dt_s=1.0, speed_kts=None
    )
    stats = compute_stats(pts, marks=[], mark_passes=[])
    assert stats is not None
    # End-to-end haversine for sanity.
    end_to_end = _haversine_m(
        pts[0].lat, pts[0].lon, pts[-1].lat, pts[-1].lon
    )
    # Summed haversine should match end-to-end within ~1% on a straight
    # line. Slight diff comes from short-segment trig accumulation.
    assert stats.distance_m == pytest.approx(end_to_end, rel=0.02)
    # ~9 segments × 1 s = 9 s elapsed.
    assert stats.elapsed_s == pytest.approx(9.0, abs=0.001)


def test_device_speed_preferred_over_haversine_derived():
    # Place points 50 m apart at 1 s → 50 m/s ~97 kt (nonsense for a
    # boat, but unambiguous). Report device speed_kts = 5.0 instead.
    pts = straight_line_track(
        n_points=10, spacing_m=50.0, dt_s=1.0, speed_kts=5.0
    )
    stats = compute_stats(pts, marks=[], mark_passes=[])
    assert stats is not None
    # max_sog_kt should be ~5 kt (from device) and never approach 97.
    assert stats.max_sog_kt == pytest.approx(5.0, abs=0.01)


def test_gap_does_not_inflate_distance():
    # Two clusters of close-together points separated by a long gap
    # with a big positional jump (simulated screen lock).
    cluster_a = straight_line_track(
        n_points=5, spacing_m=10.0, dt_s=1.0, speed_kts=2.0
    )
    # 120s gap, jump 4 km east — exactly the failure mode we observed.
    cluster_b_start = tp(
        t_offset_s=cluster_a[-1].recorded_at.timestamp()
        - BASE_T.timestamp()
        + 120.0,
        east_m=4000.0,
        speed_kts=2.0,
    )
    cluster_b = [cluster_b_start] + [
        tp(
            t_offset_s=(cluster_b_start.recorded_at - BASE_T).total_seconds()
            + i,
            east_m=4000.0 + i * 10,
            speed_kts=2.0,
        )
        for i in range(1, 5)
    ]
    pts = cluster_a + cluster_b
    stats = compute_stats(pts, marks=[], mark_passes=[])
    assert stats is not None
    # Without gap skipping the haversine would be ~4 km. With skipping
    # it should be the sum of the two short clusters (~40 m + ~40 m).
    assert stats.distance_m < 200.0


def test_moving_vs_stopped_threshold():
    # 10 points, half at 1 kt (moving), half at 0.1 kt (stopped).
    moving = [
        tp(t_offset_s=i, east_m=i * 0.5, speed_kts=1.0) for i in range(5)
    ]
    stopped = [
        tp(t_offset_s=5 + i, east_m=2.5, speed_kts=0.1) for i in range(5)
    ]
    pts = moving + stopped
    stats = compute_stats(pts, marks=[], mark_passes=[])
    assert stats is not None
    # Every segment after the first contributes either to moving or
    # stopped; the first point doesn't contribute time. There are 9
    # segments. Moving samples are at indices 1..4 (4 segs), stopped at
    # 5..9 (5 segs).
    assert stats.moving_s == pytest.approx(4.0, abs=0.001)
    assert stats.stopped_s == pytest.approx(5.0, abs=0.001)


# ─── Leg splits ───────────────────────────────────────────────────────


def _mark_at(east_m: float, north_m: float = 0.0, name: str | None = None) -> dict:
    m = {
        "lat": REF_LAT + m_to_dlat(north_m),
        "lon": REF_LON + m_to_dlon(east_m),
    }
    if name is not None:
        m["name"] = name
    return m


def _pass_at(
    *, mark_index: int, t_offset_s: float, east_m: float, north_m: float = 0.0
) -> dict:
    return {
        "mark_index": mark_index,
        "ts": (BASE_T + timedelta(seconds=t_offset_s)).isoformat(),
        "lat": REF_LAT + m_to_dlat(north_m),
        "lon": REF_LON + m_to_dlon(east_m),
    }


def test_legs_split_at_mark_passes_with_finish_label():
    # Two marks. Boat sails east 100 m to mark 0 (in 20 s), then
    # another 100 m to mark 1 (in 20 s).
    pts = [
        tp(t_offset_s=i, east_m=i * 5.0, speed_kts=10.0) for i in range(40)
    ]
    marks = [_mark_at(east_m=100.0, name="A"), _mark_at(east_m=200.0, name="B")]
    passes = [
        _pass_at(mark_index=0, t_offset_s=20.0, east_m=100.0),
        _pass_at(mark_index=1, t_offset_s=39.0, east_m=200.0),
    ]
    stats = compute_stats(pts, marks=marks, mark_passes=passes)
    assert stats is not None
    assert len(stats.legs) == 2
    assert stats.legs[0].from_label == "Start"
    assert stats.legs[0].to_label == "A"
    # Final leg ends with Finish, not the mark name, when all rounded.
    assert stats.legs[1].to_label == "Finish"
    # Distances should be roughly equal for the two halves.
    assert stats.legs[0].distance_m == pytest.approx(stats.legs[1].distance_m, rel=0.1)


def test_dnf_legs_omit_phantom_finish():
    # Three marks but only mark 0 rounded.
    pts = [
        tp(t_offset_s=i, east_m=i * 5.0, speed_kts=10.0) for i in range(40)
    ]
    marks = [
        _mark_at(east_m=100.0, name="A"),
        _mark_at(east_m=200.0, name="B"),
        _mark_at(east_m=300.0, name="C"),
    ]
    passes = [_pass_at(mark_index=0, t_offset_s=20.0, east_m=100.0)]
    stats = compute_stats(pts, marks=marks, mark_passes=passes)
    assert stats is not None
    assert len(stats.legs) == 1
    # Only one leg; its label is the mark name, not Finish.
    assert stats.legs[0].to_label == "A"


def test_leg_0_anchors_to_race_start_at_when_earlier():
    # Race start 60s before the first track point — leg 0 should be
    # 60 + 20 = 80s long.
    pts = [
        tp(t_offset_s=i, east_m=i * 5.0, speed_kts=10.0) for i in range(40)
    ]
    marks = [_mark_at(east_m=100.0, name="A")]
    passes = [_pass_at(mark_index=0, t_offset_s=20.0, east_m=100.0)]
    race_start = BASE_T - timedelta(seconds=60)
    stats = compute_stats(
        pts, marks=marks, mark_passes=passes, race_start_at=race_start
    )
    assert stats is not None
    assert stats.legs[0].elapsed_s == pytest.approx(80.0, abs=0.001)


def test_leg_unnamed_mark_falls_back_to_mark_n_label():
    pts = [
        tp(t_offset_s=i, east_m=i * 5.0, speed_kts=10.0) for i in range(40)
    ]
    marks = [_mark_at(east_m=100.0), _mark_at(east_m=200.0)]
    passes = [
        _pass_at(mark_index=0, t_offset_s=20.0, east_m=100.0),
        _pass_at(mark_index=1, t_offset_s=39.0, east_m=200.0),
    ]
    stats = compute_stats(pts, marks=marks, mark_passes=passes)
    assert stats is not None
    assert stats.legs[0].to_label == "Mark 1"
    # Final leg still labelled Finish.
    assert stats.legs[1].to_label == "Finish"


# ─── Track distance in window ────────────────────────────────────────


def test_track_distance_in_window_respects_gap():
    pts = [
        tp(t_offset_s=0, east_m=0),
        tp(t_offset_s=1, east_m=10),
        tp(t_offset_s=2, east_m=20),
        tp(t_offset_s=2 + GAP_THRESHOLD_S + 1, east_m=5000),  # huge jump
        tp(t_offset_s=2 + GAP_THRESHOLD_S + 2, east_m=5010),
    ]
    d = _track_distance_in_window(
        pts,
        BASE_T,
        BASE_T + timedelta(seconds=2 + GAP_THRESHOLD_S + 5),
    )
    # Should exclude the gap segment ~5 km and only include the
    # small-spacing chunks.
    assert d < 100.0


# ─── Speed series + Douglas-Peucker ──────────────────────────────────


def test_speed_series_downsampled_below_cap():
    # 1000 points → must be <= MAX_SPEED_SAMPLES after DP.
    pts = [
        tp(t_offset_s=i, east_m=i * 1.0, speed_kts=5.0 + 0.01 * (i % 7))
        for i in range(1000)
    ]
    stats = compute_stats(pts, marks=[], mark_passes=[])
    assert stats is not None
    assert len(stats.speed_series) <= MAX_SPEED_SAMPLES


def test_speed_series_keeps_endpoints():
    pts = [
        tp(t_offset_s=i, east_m=i * 1.0, speed_kts=5.0)
        for i in range(50)
    ]
    stats = compute_stats(pts, marks=[], mark_passes=[])
    assert stats is not None
    assert stats.speed_series[0].t_offset_s == 0.0
    # Endpoint kept by DP.
    assert stats.speed_series[-1].t_offset_s == pytest.approx(49.0, abs=0.001)


def test_douglas_peucker_drops_collinear():
    # All on the line y = 0; DP should keep only the endpoints.
    series = [(float(i), 5.0) for i in range(100)]
    kept = _douglas_peucker(series, epsilon=0.01)
    assert kept == [0, 99]


def test_douglas_peucker_keeps_spikes():
    # Flat baseline with one spike at the middle.
    series = [(float(i), 5.0) for i in range(20)]
    series[10] = (10.0, 50.0)  # huge deviation
    kept = _douglas_peucker(series, epsilon=DP_EPSILON_KT)
    assert 10 in kept


# ─── Row adapter ──────────────────────────────────────────────────────


# ─── pick_handicap (D2) ─────────────────────────────────────────────


def test_pick_handicap_inshore_spin_uses_hcp():
    boat = {"hcp": 75, "dhcp": 78, "nshcp": 90, "dnshcp": 93}
    rating, key = pick_handicap(boat, "inshore", True)
    assert rating == 75 and key == "hcp"


def test_pick_handicap_inshore_nonspin_uses_nshcp():
    boat = {"hcp": 75, "nshcp": 90}
    rating, key = pick_handicap(boat, "inshore", False)
    assert rating == 90 and key == "nshcp"


def test_pick_handicap_distance_spin_uses_dhcp():
    boat = {"hcp": 75, "dhcp": 78}
    rating, key = pick_handicap(boat, "distance", True)
    assert rating == 78 and key == "dhcp"


def test_pick_handicap_distance_nonspin_uses_dnshcp():
    boat = {"dnshcp": 95}
    rating, key = pick_handicap(boat, "distance", False)
    assert rating == 95 and key == "dnshcp"


def test_pick_handicap_returns_none_when_rating_missing():
    boat = {"hcp": None, "dhcp": None}
    rating, key = pick_handicap(boat, "inshore", True)
    assert rating is None and key is None


def test_pick_handicap_returns_none_when_boat_none():
    rating, key = pick_handicap(None, "inshore", True)
    assert rating is None and key is None


def test_pick_handicap_defaults_to_inshore_for_unknown_mode():
    boat = {"hcp": 100, "dhcp": 110}
    rating, key = pick_handicap(boat, None, True)
    assert key == "hcp"


# ─── compute_stats with boat → corrected time ───────────────────────


def test_corrected_time_set_when_boat_has_rating():
    # 1000 m east at 5 kt → 1000/1852 nm ≈ 0.54 nm in ~388 s.
    pts = [
        tp(t_offset_s=i, east_m=i * 1.0, speed_kts=5.0) for i in range(389)
    ]
    boat = {"hcp": 75}
    stats = compute_stats(
        pts, marks=[], mark_passes=[],
        boat=boat, mode="inshore", uses_spinnaker=True,
    )
    assert stats is not None
    assert stats.corrected_using == "hcp"
    assert stats.rating_seconds_per_mile == 75
    # corrected = elapsed - 75 * 0.54 ≈ 388 - 40.5 ≈ 347.5
    expected = stats.elapsed_s - 75 * (stats.distance_m / 1852.0)
    assert stats.corrected_time_s == pytest.approx(expected, rel=0.01)


def test_corrected_time_none_when_no_boat():
    pts = [
        tp(t_offset_s=i, east_m=i * 1.0, speed_kts=5.0) for i in range(50)
    ]
    stats = compute_stats(pts, marks=[], mark_passes=[])
    assert stats is not None
    assert stats.corrected_time_s is None
    assert stats.corrected_using is None


def test_corrected_time_none_when_rating_null():
    pts = [
        tp(t_offset_s=i, east_m=i * 1.0, speed_kts=5.0) for i in range(50)
    ]
    boat = {"hcp": None}
    stats = compute_stats(
        pts, marks=[], mark_passes=[],
        boat=boat, mode="inshore", uses_spinnaker=True,
    )
    assert stats is not None
    assert stats.corrected_time_s is None


def test_corrected_time_clamps_at_zero_for_fast_boats():
    # Move 100 m in 1 s → ~0.054 nm, elapsed ~1s, rating 500 → very
    # negative corrected. Clamp to 0.
    pts = [
        tp(t_offset_s=0, east_m=0, speed_kts=200),
        tp(t_offset_s=1, east_m=100, speed_kts=200),
    ]
    boat = {"hcp": 500}
    stats = compute_stats(
        pts, marks=[], mark_passes=[],
        boat=boat, mode="inshore", uses_spinnaker=True,
    )
    assert stats is not None
    assert stats.corrected_time_s == 0.0


def test_corrected_time_distance_mode_uses_dhcp():
    pts = [
        tp(t_offset_s=i, east_m=i * 1.0, speed_kts=5.0) for i in range(50)
    ]
    boat = {"hcp": 75, "dhcp": 78}
    stats = compute_stats(
        pts, marks=[], mark_passes=[],
        boat=boat, mode="distance", uses_spinnaker=True,
    )
    assert stats is not None
    assert stats.corrected_using == "dhcp"
    assert stats.rating_seconds_per_mile == 78


# ─── Row adapter (existing test, kept) ──────────────────────────────


def test_track_points_from_rows_handles_iso_and_dt():
    rows = [
        {
            "recorded_at": BASE_T,
            "lat": REF_LAT,
            "lon": REF_LON,
            "speed_kts": 3.2,
            "heading_deg": 45.0,
        },
        {
            "recorded_at": (BASE_T + timedelta(seconds=1)).isoformat(),
            "lat": REF_LAT,
            "lon": REF_LON,
            "speed_kts": None,
            "heading_deg": None,
        },
    ]
    out = track_points_from_rows(rows)
    assert len(out) == 2
    assert out[0].speed_kts == pytest.approx(3.2)
    assert out[1].speed_kts is None
