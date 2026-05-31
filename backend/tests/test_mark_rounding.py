"""Pure-function tests for app/services/mark_rounding.py.

No DB, no network, no router — just the detector. Track points are
synthesised geometrically: we pick a mark and walk a straight line
through it, varying the closest-approach distance to drive the
algorithm deterministically.

We use latitudes around the Cook County Sailing area (Lake Michigan,
~42.0°N, -87.7°E) so the haversine math behaves the same as production.

Test scope — v3 streaming sequential CPA (2026-05-30):
  * Tight pass detected.
  * Wide pass at distance threshold detected (the v2-failure case that
    motivated v3 — see ``sailline-docs/2026-05-30_session.md``).
  * Wide pass at inshore threshold NOT detected.
  * Sequential ordering enforced (later marks ignored until earlier ones
    are passed).
  * Multi-lap via repeated marks.
  * DNF (no emit, detector stays not-done).
  * Resume from persisted next_mark_index.
  * GPS jitter near CPA doesn't double-emit.
  * Per-mark threshold list honored; length mismatch and non-positive
    values rejected.
  * Integration: today's Colors (Bravo) GPX track produces 7/7 passes
    in distance mode (the canonical real-world fixture for v3).
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.mark_rounding import (
    DEFAULT_DISTANCE_THRESHOLD_M,
    DEFAULT_INSHORE_THRESHOLD_M,
    DEFAULT_RADIUS_M,
    DEPART_CONFIRM_SAMPLES,
    FINAL_MARK_BONUS_M,
    FINAL_MARK_RADIUS_M,
    Mark,
    MarkRoundingDetector,
    Point,
    compute_passes,
    radii_for_course,
    thresholds_for_course,
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
    span_m: float = 600.0,
    n: int = 31,
    bearing_deg: float = 90.0,
    t0: float = 0.0,
    dt_s: float = 1.0,
) -> list[Point]:
    """Generate a straight-line track that passes ``closest_m`` from the
    mark at its midpoint.

    Bearing is the direction of travel relative to true north (90° = due
    east). Track is evenly spaced along that bearing, centred on the
    closest-approach point.

    Defaults give 600 m of travel, 21 m per step — plenty of room for
    the v3 detector to see at least DEPART_CONFIRM_SAMPLES strictly-
    increasing distances after CPA, even with the wide (200 m+) passes
    we test for distance-racing scenarios.
    """
    perp_bearing = (bearing_deg + 90.0) % 360.0
    cap_lat, cap_lon = _offset(mark.lat, mark.lon, perp_bearing, closest_m)

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


# ─── Core v3 algorithm tests ───────────────────────────────────────────


def test_tight_pass_detected_in_inshore_mode():
    """Inside the inshore 100 m threshold — pass detected at CPA."""
    mark = Mark(REF_LAT, REF_LON)
    track = line_through(mark, closest_m=10.0)

    passes = compute_passes([mark], track, threshold_m=DEFAULT_INSHORE_THRESHOLD_M)

    assert len(passes) == 1
    assert passes[0].mark_index == 0
    midpoint = track[len(track) // 2]
    assert passes[0].ts == midpoint.ts
    assert passes[0].lat == midpoint.lat


def test_wide_pass_detected_in_distance_mode():
    """The v3 win: 200 m pass detected with the distance-mode 250 m
    threshold. This is the failure mode that motivated v3 — see
    ``sailline-docs/2026-05-30_session.md`` (Harrison-Dever Crib was
    passed at 208 m and the old detector missed it entirely)."""
    mark = Mark(REF_LAT, REF_LON)
    track = line_through(mark, closest_m=200.0)

    passes = compute_passes(
        [mark], track, threshold_m=DEFAULT_DISTANCE_THRESHOLD_M,
    )

    assert len(passes) == 1
    midpoint = track[len(track) // 2]
    assert passes[0].ts == midpoint.ts


def test_wide_pass_not_detected_in_inshore_mode():
    """Same 200 m pass at the inshore 100 m threshold — does NOT emit.
    Sequential ordering then keeps subsequent marks gated, preserving
    "did the race actually round all marks" as a meaningful signal."""
    mark = Mark(REF_LAT, REF_LON)
    track = line_through(mark, closest_m=200.0)

    passes = compute_passes(
        [mark], track, threshold_m=DEFAULT_INSHORE_THRESHOLD_M,
    )

    assert passes == []


def test_very_wide_pass_never_detected():
    """800 m pass with a 250 m distance threshold — well outside. No emit."""
    mark = Mark(REF_LAT, REF_LON)
    track = line_through(mark, closest_m=800.0)

    passes = compute_passes(
        [mark], track, threshold_m=DEFAULT_DISTANCE_THRESHOLD_M,
    )

    assert passes == []


def test_two_mark_course_in_order():
    """Round mark A then mark B. Detector should emit in order, with
    timestamps reflecting actual passage time."""
    a = Mark(REF_LAT, REF_LON)
    b_lat, b_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=2000.0)
    b = Mark(b_lat, b_lon)

    track_a = line_through(a, closest_m=20.0, t0=0.0)
    t_after_a = track_a[-1].ts.timestamp() - track_a[0].ts.timestamp() + 60.0
    track_b = line_through(b, closest_m=20.0, t0=t_after_a)

    passes = compute_passes([a, b], track_a + track_b)

    assert [p.mark_index for p in passes] == [0, 1]
    assert passes[0].ts < passes[1].ts


def test_passes_through_later_mark_first_are_ignored():
    """Sail past mark B before rounding A → must NOT count B yet.

    With v3's removal of the radius gate, this property leans entirely
    on sequential ordering — the detector is only ever watching the
    next-expected mark, so an incidental close pass to a later mark is
    invisible to it."""
    a_lat, a_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=2000.0)
    a = Mark(a_lat, a_lon)
    b = Mark(REF_LAT, REF_LON)

    leg1 = line_through(b, closest_m=20.0, t0=0.0)
    leg2 = line_through(a, closest_m=20.0, t0=200.0)
    leg3 = line_through(b, closest_m=20.0, t0=500.0)

    passes = compute_passes([a, b], leg1 + leg2 + leg3)

    assert [p.mark_index for p in passes] == [0, 1]


def test_multilap_via_repeated_marks():
    """Beer-can: start = finish, two laps. Course list repeats the
    start mark for each lap so the detector treats them as distinct
    entries in sequence — exactly what the router should provide."""
    s = Mark(REF_LAT, REF_LON)
    w_lat, w_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=2000.0)
    w = Mark(w_lat, w_lon)

    course = [s, w, s, w, s]

    legs = [
        line_through(s, closest_m=20.0, t0=0.0),
        line_through(w, closest_m=20.0, t0=300.0),
        line_through(s, closest_m=20.0, t0=600.0),
        line_through(w, closest_m=20.0, t0=900.0),
        line_through(s, closest_m=20.0, t0=1200.0),
    ]
    track = [p for leg in legs for p in leg]

    passes = compute_passes(course, track)

    assert [p.mark_index for p in passes] == [0, 1, 2, 3, 4]


def test_dnf_track_never_completes():
    """If the boat bails after rounding mark 0, mark 1 stays unrounded
    and the detector reports done=False — what auto-stop relies on to
    NOT trigger."""
    a = Mark(REF_LAT, REF_LON)
    b_lat, b_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=2000.0)
    b = Mark(b_lat, b_lon)

    track = line_through(a, closest_m=20.0)

    det = MarkRoundingDetector([a, b])
    passes = det.feed_batch(track)

    assert [p.mark_index for p in passes] == [0]
    assert det.next_mark_index == 1
    assert det.done is False


def test_resume_from_persisted_state():
    """Simulates the router pattern: previous batches already detected
    pass 0; new batch only contains points around mark 1. Detector is
    constructed with ``next_mark_index=1`` and should NOT re-emit pass
    0 just because the new batch skims near mark 0 again."""
    a = Mark(REF_LAT, REF_LON)
    b_lat, b_lon = _offset(REF_LAT, REF_LON, bearing_deg=0.0, dist_m=2000.0)
    b = Mark(b_lat, b_lon)

    near_a_again = line_through(a, closest_m=20.0, t0=100.0)
    near_b = line_through(b, closest_m=20.0, t0=300.0)

    det = MarkRoundingDetector([a, b], next_mark_index=1)
    passes = det.feed_batch(near_a_again + near_b)

    assert [p.mark_index for p in passes] == [1]


def test_gps_jitter_near_cpa_does_not_double_count():
    """Dense sampling through CPA with mild jitter — exactly one pass.
    The running-minimum reset on each new closer sample plus the
    departing-count guard handle this without special-casing."""
    mark = Mark(REF_LAT, REF_LON)
    track = line_through(mark, closest_m=5.0, span_m=400.0, n=81)

    passes = compute_passes([mark], track, threshold_m=DEFAULT_INSHORE_THRESHOLD_M)

    assert len(passes) == 1


def test_asymmetric_cpa_emits_at_min_distance_point():
    """Asymmetric track: walks closer-then-farther so CPA is clearly not
    a midpoint. Pass timestamp must be the CPA sample."""
    mark = Mark(REF_LAT, REF_LON)
    base = pt(REF_LAT, REF_LON, 0).ts
    distances = [60, 40, 20, 10, 5, 10, 20, 40, 60, 80, 100]
    track = [
        Point(
            lat=REF_LAT,
            lon=REF_LON + m_to_dlon(d),
            ts=base.replace(microsecond=0) + timedelta(seconds=i),
        )
        for i, d in enumerate(distances)
    ]

    passes = compute_passes([mark], track, threshold_m=DEFAULT_INSHORE_THRESHOLD_M)

    assert len(passes) == 1
    assert passes[0].ts == track[4].ts  # the 5-metre sample
    assert passes[0].lon == track[4].lon


# ─── Parameter validation ─────────────────────────────────────────────


def test_threshold_must_be_positive():
    with pytest.raises(ValueError):
        MarkRoundingDetector([Mark(REF_LAT, REF_LON)], threshold_m=0)


def test_radius_kw_back_compat():
    """Old ``radius_m`` keyword still accepted (maps to threshold_m)."""
    mark = Mark(REF_LAT, REF_LON)
    track = line_through(mark, closest_m=20.0)
    passes = compute_passes([mark], track, radius_m=100.0)
    assert len(passes) == 1


def test_per_mark_threshold_length_mismatch_rejects():
    a = Mark(REF_LAT, REF_LON)
    b = Mark(REF_LAT + 0.01, REF_LON)
    with pytest.raises(ValueError):
        MarkRoundingDetector([a, b], threshold_m=[100.0])
    with pytest.raises(ValueError):
        MarkRoundingDetector([a, b], threshold_m=[100.0, 100.0, 100.0])


def test_per_mark_thresholds_must_all_be_positive():
    a = Mark(REF_LAT, REF_LON)
    b = Mark(REF_LAT + 0.01, REF_LON)
    with pytest.raises(ValueError):
        MarkRoundingDetector([a, b], threshold_m=[100.0, 0.0])


def test_depart_confirm_samples_must_be_positive():
    with pytest.raises(ValueError):
        MarkRoundingDetector(
            [Mark(REF_LAT, REF_LON)], depart_confirm_samples=0,
        )


# ─── thresholds_for_course / back-compat aliases ──────────────────────


def test_thresholds_for_course_distance_mode():
    """Multi-mark distance course: DEFAULT_DISTANCE for intermediate
    marks, +FINAL_MARK_BONUS_M for the final."""
    out = thresholds_for_course(4, mode="distance")
    assert out == [
        DEFAULT_DISTANCE_THRESHOLD_M,
        DEFAULT_DISTANCE_THRESHOLD_M,
        DEFAULT_DISTANCE_THRESHOLD_M,
        DEFAULT_DISTANCE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]


def test_thresholds_for_course_inshore_mode():
    out = thresholds_for_course(3, mode="inshore")
    assert out == [
        DEFAULT_INSHORE_THRESHOLD_M,
        DEFAULT_INSHORE_THRESHOLD_M,
        DEFAULT_INSHORE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]


def test_thresholds_for_course_unknown_mode_defaults_to_distance():
    out = thresholds_for_course(2, mode="nonsense")
    assert out == [
        DEFAULT_DISTANCE_THRESHOLD_M,
        DEFAULT_DISTANCE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]


def test_thresholds_for_course_edge_cases():
    assert thresholds_for_course(0) == []
    assert thresholds_for_course(1) == [
        DEFAULT_DISTANCE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]


def test_radii_for_course_back_compat_returns_distance_thresholds():
    """``radii_for_course`` is a legacy alias — defaults to distance-mode
    thresholds for safety (missing a mark is worse than tripping early
    on a tight inshore course, and sequential ordering protects
    against the false-positive case in practice)."""
    out = radii_for_course(3)
    assert out == [
        DEFAULT_DISTANCE_THRESHOLD_M,
        DEFAULT_DISTANCE_THRESHOLD_M,
        DEFAULT_DISTANCE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]


def test_threshold_constants_tripwire():
    """Surfaces accidental constant changes. Bump these only with a
    matching docstring / migration plan."""
    assert DEFAULT_DISTANCE_THRESHOLD_M == 250.0
    assert DEFAULT_INSHORE_THRESHOLD_M == 100.0
    assert FINAL_MARK_BONUS_M == 50.0
    assert DEPART_CONFIRM_SAMPLES == 3
    # Legacy aliases must keep mapping to the inshore numbers so anyone
    # still importing the old names gets the same threshold they had
    # under v2's "intermediate=50, final=75" policy upgraded to "100/150".
    # (v2 50/75 was too tight for ANY real racing; v3 100/150 is the
    # right inshore default.)
    assert DEFAULT_RADIUS_M == DEFAULT_INSHORE_THRESHOLD_M
    assert FINAL_MARK_RADIUS_M == DEFAULT_INSHORE_THRESHOLD_M + FINAL_MARK_BONUS_M


# ─── Real-world fixture: 2026-05-30 Colors (Bravo) ───────────────────


def _load_garmin_gpx(path: Path) -> list[Point]:
    """Parse a Garmin Connect GPX track into detector Points."""
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    tree = ET.parse(path)
    out: list[Point] = []
    for tp in tree.iter("{http://www.topografix.com/GPX/1/1}trkpt"):
        lat = float(tp.get("lat"))
        lon = float(tp.get("lon"))
        t_el = tp.find("g:time", ns)
        ts = datetime.fromisoformat(t_el.text.replace("Z", "+00:00"))
        out.append(Point(lat=lat, lon=lon, ts=ts))
    return out


# Marks from the 2026-05-30 Colors (Bravo) race row, in order.
_BRAVO_MARKS = [
    Mark(41.880000, -87.574333),   # Start
    Mark(41.962333, -87.540000),   # CCYC E2
    Mark(42.002000, -87.551167),   # CCYC T
    Mark(41.966667, -87.591667),   # Wilson Crib
    Mark(41.916667, -87.571667),   # Harrison-Dever Crib
    Mark(41.892500, -87.563000),   # Purdue Met Buoy 1A
    Mark(41.880000, -87.574330),   # Finish
]


def _bravo_fixture_path() -> Path:
    """The 2026-05-30 Garmin track lives under backend/tests/fixtures.
    Test is skipped if not present (the fixture lives outside CI for
    privacy until a redacted version is checked in)."""
    return Path(__file__).parent / "fixtures" / "colors_bravo_20260530.gpx"


@pytest.mark.skipif(
    not _bravo_fixture_path().exists(),
    reason="Bravo fixture not checked in — see backend/tests/fixtures/README.md",
)
def test_real_world_colors_bravo_detects_all_seven_marks():
    """Canonical real-world regression: the 2026-05-30 Colors (Bravo)
    distance race that v2 recorded zero passes for. v3 in distance mode
    must detect 7/7 marks in order. If this drops, something regressed
    in either the algorithm or the threshold defaults."""
    points = _load_garmin_gpx(_bravo_fixture_path())
    thresholds = thresholds_for_course(len(_BRAVO_MARKS), mode="distance")
    passes = compute_passes(_BRAVO_MARKS, points, threshold_m=thresholds)
    assert [p.mark_index for p in passes] == list(range(len(_BRAVO_MARKS)))
    # Each pass must come strictly after the previous.
    for i in range(1, len(passes)):
        assert passes[i].ts > passes[i - 1].ts


@pytest.mark.skipif(
    not _bravo_fixture_path().exists(),
    reason="Bravo fixture not checked in — see backend/tests/fixtures/README.md",
)
def test_real_world_colors_bravo_inshore_mode_misses_distance_passes():
    """Same track in inshore (100 m) mode misses the wide passage
    marks. Documents the difference between modes and explains why the
    router must pass the race's mode through to the detector."""
    points = _load_garmin_gpx(_bravo_fixture_path())
    thresholds = thresholds_for_course(len(_BRAVO_MARKS), mode="inshore")
    passes = compute_passes(_BRAVO_MARKS, points, threshold_m=thresholds)
    # Inshore mode catches the near-shore marks but misses Harrison-
    # Dever (208 m wide pass) — detection stalls at index 4 onwards.
    detected = [p.mark_index for p in passes]
    assert detected == [0, 1, 2, 3]
