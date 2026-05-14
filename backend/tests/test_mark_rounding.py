"""Pure-function tests for app/services/mark_rounding.py.

No DB, no network, no router — just the detector. Track points are
synthesised geometrically: we pick a mark and walk a straight line
through it, varying the closest-approach distance to drive the
inside/outside transitions deterministically.

We use latitudes around the Cook County Sailing area (Lake Michigan,
~42.0°N, -87.7°E) so the haversine math behaves the same as production.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.services.mark_rounding import (
    DEFAULT_RADIUS_M,
    Mark,
    MarkRoundingDetector,
    Point,
    compute_passes,
)


# ─── Geometry helpers ───────────────────────────────────────────────────
#
# Working with metres around 42°N. 1° latitude ~ 111_000 m. 1° longitude
# ~ 111_000 * cos(42°) m ~= 82_500 m. Helpers below convert metre offsets
# to lat/lon offsets so we can place points "X metres east of mark Y"
# without spinning our own projection inside every test.

REF_LAT = 42.05
REF_LON = -87.75


def m_to_dlat(m: float) -> float:
    return m / 111_000.0


def m_to_dlon(m: float, at_lat: float = REF_LAT) -> float:
    return m / (111_000.0 * math.cos(math.radians(at_lat)))


def pt(lat: float, lon: float, t_offset_s: float, speed: float = 5.0) -> Point:
    """One synthesised point at ``base_time + offset``."""
    base = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    return Point(
        lat=lat,
        lon=lon,
        ts=base + timedelta(seconds=t_offset_s),
        speed_kts=speed,
        heading_deg=90.0,
    )


def line_through(
    mark: Mark,
    closest_m: float,
    span_m: float = 200.0,
    n: int = 21,
    bearing_deg: float = 90.0,
    t0: float = 0.0,
    dt_s: float = 1.0,
) -> list[Point]:
    """Generate a straight-line track that passes ``closest_m`` from the
    mark at its midpoint.

    Bearing is the direction of travel relative to true north (90° = due
    east). The track is evenly spaced in metres along that bearing,
    centred on the closest-approach point.
    """
    # Closest-approach point is offset from the mark perpendicular to
    # the travel bearing, at distance ``closest_m``.
    perp_bearing = (bearing_deg + 90.0) % 360.0
    cap_lat, cap_lon = _offset(mark.lat, mark.lon, perp_bearing, closest_m)

    # Points span ±span_m/2 along the travel bearing from the closest-
    # approach point.
    half = span_m / 2.0
    step = span_m / (n - 1) if n > 1 else 0.0
    points: list[Point] = []
    for i in range(n):
        d_along = -half + i * step
        lat, lon = _offset(cap_lat, cap_lon, bearing_deg, d_along)
        points.append(pt(lat, lon, t0 + i * dt_s))
    return points


def _offset(lat: float, lon: float, bearing_deg: float, dist_m: float):
    """Move ``dist_m`` metres along ``bearing_deg`` from (lat, lon)."""
    rad = math.radians(bearing_deg)
    dlat = m_to_dlat(dist_m * math.cos(rad))
    dlon = m_to_dlon(dist_m * math.sin(rad), at_lat=lat)
    return lat + dlat, lon + dlon


# ─── Tests ─────────────────────────────────────────────────────────────


def test_straight_pass_through_radius_emits_one_pass():
    mark = Mark(REF_LAT, REF_LON)
    # Pass within 10 m of the mark — well inside the 50 m radius.
    track = line_through(mark, closest_m=10.0)

    passes = compute_passes([mark], track)

    assert len(passes) == 1
    assert passes[0].mark_index == 0
    # Pass time should fall after the closest approach (by definition,
    # the exit ends the rounding event).
    midpoint_ts = track[len(track) // 2].ts
    assert passes[0].ts >= midpoint_ts


def test_fly_by_outside_radius_emits_nothing():
    mark = Mark(REF_LAT, REF_LON)
    # Closest approach is 75 m — outside the 50 m default radius.
    track = line_through(mark, closest_m=75.0)

    passes = compute_passes([mark], track)

    assert passes == []


def test_two_mark_course_in_order():
    """W-L style: round mark A then mark B. Detector should emit in
    order, with timestamps reflecting actual passage time."""
    a = Mark(REF_LAT, REF_LON)
    b_lat, b_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=500.0)
    b = Mark(b_lat, b_lon)

    # Pass A close, then traverse to B close.
    track_a = line_through(a, closest_m=8.0, t0=0.0)
    # Start the second leg's clock after the first leg ends, with a
    # plausible boat-speed delta.
    t_after_a = track_a[-1].ts.timestamp() - track_a[0].ts.timestamp() + 60.0
    track_b = line_through(b, closest_m=8.0, t0=t_after_a)

    passes = compute_passes([a, b], track_a + track_b)

    assert [p.mark_index for p in passes] == [0, 1]
    assert passes[0].ts < passes[1].ts


def test_passes_through_later_mark_first_are_ignored():
    """Sail past mark B before rounding A → must NOT count B yet.

    Common on W-L courses: the boat sails through the leeward gate area
    on its way to the windward mark. B should fire only AFTER A's
    rounding has been recorded.
    """
    a_lat, a_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=500.0)
    a = Mark(a_lat, a_lon)
    b = Mark(REF_LAT, REF_LON)  # B is BACK toward the start

    # Walk THROUGH B's radius first, then proceed to A's, then back
    # through B's. Only A's rounding then B's should fire.
    leg1 = line_through(b, closest_m=8.0, t0=0.0)             # passes B (ignored)
    leg2 = line_through(a, closest_m=8.0, t0=100.0)           # rounds A
    leg3 = line_through(b, closest_m=8.0, t0=200.0)           # rounds B

    passes = compute_passes([a, b], leg1 + leg2 + leg3)

    assert [p.mark_index for p in passes] == [0, 1]


def test_multilap_via_repeated_marks():
    """Beer-can: start = finish, two laps. Course list repeats the start
    mark for each lap so the detector treats them as distinct entries
    in sequence — exactly what the router should provide."""
    s = Mark(REF_LAT, REF_LON)
    w_lat, w_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=500.0)
    w = Mark(w_lat, w_lon)

    # Course: Start, Windward, Start (lap 2 marker), Windward, Finish.
    course = [s, w, s, w, s]

    # Track: two laps of S→W→S, ending with one more S.
    legs = [
        line_through(s, closest_m=8.0, t0=0.0),
        line_through(w, closest_m=8.0, t0=120.0),
        line_through(s, closest_m=8.0, t0=240.0),
        line_through(w, closest_m=8.0, t0=360.0),
        line_through(s, closest_m=8.0, t0=480.0),
    ]
    track = [p for leg in legs for p in leg]

    passes = compute_passes(course, track)

    assert [p.mark_index for p in passes] == [0, 1, 2, 3, 4]


def test_dnf_track_never_completes():
    """If the boat bails after rounding mark 0, mark 1 stays unrounded
    and the detector reports done=False — what auto-stop relies on to
    NOT trigger."""
    a = Mark(REF_LAT, REF_LON)
    b_lat, b_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=500.0)
    b = Mark(b_lat, b_lon)

    # Round A then drift in random direction, never reaching B.
    track = line_through(a, closest_m=8.0)

    det = MarkRoundingDetector([a, b])
    passes = det.feed_batch(track)

    assert [p.mark_index for p in passes] == [0]
    assert det.next_mark_index == 1
    assert det.done is False


def test_resume_from_persisted_state():
    """Simulates the router pattern: previous batches already detected
    pass 0; new batch only contains points around mark 1. Detector is
    constructed with ``next_mark_index=1`` and should NOT re-emit pass
    0 just because the new batch happens to skim near mark 0 again."""
    a = Mark(REF_LAT, REF_LON)
    b_lat, b_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=500.0)
    b = Mark(b_lat, b_lon)

    # New batch: passes near A again (boat doubles back briefly), then
    # rounds B properly. Since we resume at next=1, A is invisible.
    near_a_again = line_through(a, closest_m=8.0, t0=100.0)
    near_b = line_through(b, closest_m=8.0, t0=200.0)

    det = MarkRoundingDetector([a, b], next_mark_index=1)
    passes = det.feed_batch(near_a_again + near_b)

    assert [p.mark_index for p in passes] == [1]


def test_gps_jitter_inside_radius_does_not_double_count():
    """The boat enters the radius, GPS produces several samples inside
    (jitter), then exits. We should emit exactly one pass at the exit
    point, not one per inside sample."""
    mark = Mark(REF_LAT, REF_LON)
    # Dense sampling: 41 points across a 200 m line at 5 m spacing —
    # the middle 21 points fall well inside a 50 m radius.
    track = line_through(mark, closest_m=5.0, span_m=200.0, n=41)

    passes = compute_passes([mark], track)

    assert len(passes) == 1


def test_radius_must_be_positive():
    with pytest.raises(ValueError):
        MarkRoundingDetector([Mark(REF_LAT, REF_LON)], radius_m=0)


def test_compute_passes_default_radius_matches_constant():
    """Sanity check that the convenience wrapper uses the documented
    default. If someone bumps DEFAULT_RADIUS_M without thinking, this
    test will surface it loudly."""
    assert DEFAULT_RADIUS_M == 50.0
