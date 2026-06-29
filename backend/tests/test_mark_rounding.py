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
    """The v3 win: 200 m pass detected with the distance-mode 400 m
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
    """800 m pass with a 400 m distance threshold — well outside. No emit."""
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
    assert DEFAULT_DISTANCE_THRESHOLD_M == 400.0
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


# ─── Cross-batch state persistence (2026-06-04) ─────────────────────────


def test_dump_and_restore_round_trip_preserves_state():
    """dump_state → restore_state should produce a detector that
    behaves identically to the source. Sanity check before relying on
    persistence across batches in the realistic fixture below."""
    marks = [Mark(lat=REF_LAT, lon=REF_LON)]
    src = MarkRoundingDetector(marks, threshold_m=100.0)
    # Feed two approach samples to push some traversal state in.
    src.feed(pt(REF_LAT, REF_LON + m_to_dlon(200), 0))
    src.feed(pt(REF_LAT, REF_LON + m_to_dlon(150), 1))

    state = src.dump_state()
    assert state is not None
    # Round-trip through JSON to mirror the JSONB transport.
    import json
    state = json.loads(json.dumps(state))

    restored = MarkRoundingDetector(marks, threshold_m=100.0, state=state)
    # Continue the approach + depart on both — they must emit identically.
    extra = [
        pt(REF_LAT, REF_LON + m_to_dlon(50), 2),
        pt(REF_LAT, REF_LON, 3),                 # CPA
        pt(REF_LAT, REF_LON + m_to_dlon(40), 4),
        pt(REF_LAT, REF_LON + m_to_dlon(80), 5),
        pt(REF_LAT, REF_LON + m_to_dlon(120), 6),
    ]
    src_passes = []
    rst_passes = []
    for p in extra:
        s = src.feed(p)
        r = restored.feed(p)
        if s:
            src_passes.append(s)
        if r:
            rst_passes.append(r)
    assert [p.mark_index for p in src_passes] == [p.mark_index for p in rst_passes]
    assert [p.ts for p in src_passes] == [p.ts for p in rst_passes]


def test_dump_state_is_none_for_fresh_detector():
    """A detector that hasn't seen a sample (or just emitted a pass)
    should report no traversal state to persist. Lets the caller write
    SQL NULL — keeps the column clean."""
    det = MarkRoundingDetector(
        [Mark(lat=REF_LAT, lon=REF_LON)], threshold_m=100.0,
    )
    assert det.dump_state() is None


def test_one_sample_batches_without_state_misses_pass():
    """Regression: with no cross-batch state persistence, feeding one
    sample per batch fails to emit even on a textbook approach. This
    documents the bug that motivated migration 0020 — production behaviour
    before the fix, and what would happen if a caller forgot to thread
    ``detector_state`` through."""
    marks = [Mark(lat=REF_LAT, lon=REF_LON)]
    threshold = 100.0
    # Approach + CPA + 5 increasing departing samples.
    samples = [
        pt(REF_LAT, REF_LON + m_to_dlon(50), 0),
        pt(REF_LAT, REF_LON + m_to_dlon(20), 1),
        pt(REF_LAT, REF_LON, 2),                 # CPA
        pt(REF_LAT, REF_LON + m_to_dlon(30), 3),
        pt(REF_LAT, REF_LON + m_to_dlon(60), 4),
        pt(REF_LAT, REF_LON + m_to_dlon(90), 5),
        pt(REF_LAT, REF_LON + m_to_dlon(120), 6),
    ]
    emitted = []
    for s in samples:
        # Each batch is a fresh detector with no state — the pre-0020
        # production behaviour.
        det = MarkRoundingDetector(marks, threshold_m=threshold)
        passes = det.feed_batch([s])
        emitted.extend(passes)
    assert emitted == [], (
        "Without cross-batch state, depart-confirm never accumulates and "
        "the detector misses the pass. This is the bug 0020 fixes."
    )


def test_one_sample_batches_with_state_detects_pass():
    """With dump_state / restore_state threaded through every batch,
    the same one-sample-per-batch cadence detects the pass correctly.
    This is the post-0020 production path."""
    marks = [Mark(lat=REF_LAT, lon=REF_LON)]
    threshold = 100.0
    samples = [
        pt(REF_LAT, REF_LON + m_to_dlon(50), 0),
        pt(REF_LAT, REF_LON + m_to_dlon(20), 1),
        pt(REF_LAT, REF_LON, 2),                 # CPA
        pt(REF_LAT, REF_LON + m_to_dlon(30), 3),
        pt(REF_LAT, REF_LON + m_to_dlon(60), 4),
        pt(REF_LAT, REF_LON + m_to_dlon(90), 5),
        pt(REF_LAT, REF_LON + m_to_dlon(120), 6),
    ]
    persisted: dict | None = None
    next_idx = 0
    emitted = []
    for s in samples:
        det = MarkRoundingDetector(
            marks,
            threshold_m=threshold,
            next_mark_index=next_idx,
            state=persisted,
        )
        new_passes = det.feed_batch([s])
        emitted.extend(new_passes)
        next_idx += len(new_passes)
        persisted = det.dump_state()
    assert [p.mark_index for p in emitted] == [0]
    # Pass should be timestamped at the CPA sample (t=2), not the depart
    # samples. Confirms the state correctly tracked the running minimum.
    assert emitted[0].ts == samples[2].ts


# ─── Beer Can Race 4 (2026-06-03) — real-world streaming fixture ─────────


_BEER_CAN_FIXTURE = (
    Path(__file__).parent / "fixtures" / "beer_can_race_4_20260603.json"
)

_BEER_CAN_MARKS = [
    Mark(lat=41.852833333333336, lon=-87.55683333333333),  # SA7 start
    Mark(lat=41.852833333333336, lon=-87.5325),            # 3
    Mark(lat=41.86566666666667, lon=-87.5395),             # 2
    Mark(lat=41.86566666666667, lon=-87.574),              # 8
    Mark(lat=41.852833333333336, lon=-87.58116666666666),  # 7  (never reached — DNF)
    Mark(lat=41.852833333333336, lon=-87.55683333333333),  # SA7 finish (never reached)
]


def _load_beer_can_points() -> list[Point]:
    """Load the Beer Can 4 telemetry. Returns Point objects in chrono
    order. Skipped at module import if the fixture isn't checked in."""
    import json as _json
    import re as _re

    raw = _json.loads(_BEER_CAN_FIXTURE.read_text())
    out: list[Point] = []
    for p in raw["points"]:
        ts_str = p["ts"]
        # Tolerate Z + truncated fractional seconds — the studio export
        # mixes both. fromisoformat is fussy about exactly 3 or 6 digits.
        ts_str = ts_str.replace("Z", "+00:00")
        m = _re.match(r"(.+?)\.(\d+)([+-]\d{2}:\d{2})", ts_str)
        if m:
            frac = (m.group(2) + "000000")[:6]
            ts_str = f"{m.group(1)}.{frac}{m.group(3)}"
        out.append(
            Point(
                lat=float(p["lat"]),
                lon=float(p["lon"]),
                ts=datetime.fromisoformat(ts_str),
                speed_kts=float(p.get("sog_kts") or 0.0),
            )
        )
    return out


@pytest.mark.skipif(
    not _BEER_CAN_FIXTURE.exists(),
    reason="Beer Can 4 fixture not checked in — see backend/tests/fixtures/README.md",
)
def test_beer_can_4_bulk_batch_detects_first_four_marks():
    """Whole-track baseline: feeding the entire trace as a single batch
    detects the 4 marks the boat actually sailed (SA7 start, 3, 2, 8)
    before DNF. Inshore mode = 100 m threshold; Mark 2 CPA was 1.1 m so
    well inside."""
    points = _load_beer_can_points()
    thresholds = thresholds_for_course(len(_BEER_CAN_MARKS), mode="inshore")
    passes = compute_passes(_BEER_CAN_MARKS, points, threshold_m=thresholds)
    assert [p.mark_index for p in passes] == [0, 1, 2, 3], (
        "Whole-track replay must catch marks 0-3. Mark 4 and 5 are not "
        "reachable on this trace (DNF + motor back to harbour)."
    )


@pytest.mark.skipif(
    not _BEER_CAN_FIXTURE.exists(),
    reason="Beer Can 4 fixture not checked in — see backend/tests/fixtures/README.md",
)
def test_beer_can_4_streaming_one_sample_batches_with_state_detects_all_marks():
    """The bug fix in action: replaying the same trace one sample per
    feed_batch call (mirroring the mobile native uploader's
    autoSyncThreshold=1 cadence), with dump_state / restore_state
    threaded between calls, detects the same 4 marks as the bulk run.

    Without the cross-batch state persistence this test would emit zero
    passes — which is exactly what production did on 2026-06-03 for
    Mark 2 (CPA was 1.1 m, but the depart-confirm samples landed in
    separate batches and the counter reset on each)."""
    import json as _json

    points = _load_beer_can_points()
    marks = _BEER_CAN_MARKS
    thresholds = thresholds_for_course(len(marks), mode="inshore")

    persisted: dict | None = None
    next_idx = 0
    emitted: list = []
    for p in points:
        det = MarkRoundingDetector(
            marks,
            threshold_m=thresholds,
            next_mark_index=next_idx,
            state=persisted,
        )
        new_passes = det.feed_batch([p])
        emitted.extend(new_passes)
        next_idx += len(new_passes)
        # Round-trip through JSON to mirror the JSONB persistence path.
        # If the dumped state can't survive JSON it can't survive PostgreSQL.
        raw = det.dump_state()
        persisted = _json.loads(_json.dumps(raw)) if raw is not None else None

    assert [p.mark_index for p in emitted] == [0, 1, 2, 3]


@pytest.mark.skipif(
    not _BEER_CAN_FIXTURE.exists(),
    reason="Beer Can 4 fixture not checked in — see backend/tests/fixtures/README.md",
)
def test_beer_can_4_streaming_one_sample_batches_without_state_misses_marks():
    """Regression guard: the same one-sample-per-batch replay WITHOUT
    cross-batch state persistence drops most or all of the passes.
    Documents the production bug for posterity — if a future change
    accidentally severs the state thread, this test surfaces it."""
    points = _load_beer_can_points()
    marks = _BEER_CAN_MARKS
    thresholds = thresholds_for_course(len(marks), mode="inshore")
    next_idx = 0
    emitted = []
    for p in points:
        det = MarkRoundingDetector(
            marks,
            threshold_m=thresholds,
            next_mark_index=next_idx,
        )
        new_passes = det.feed_batch([p])
        emitted.extend(new_passes)
        next_idx += len(new_passes)
    # Without state persistence the detector emits 0 or close to 0
    # passes — definitely fewer than the 4 the boat actually sailed.
    assert len(emitted) < 4, (
        "Without cross-batch state, the streaming cadence cannot accumulate "
        "the depart-confirm count and most marks are missed."
    )


# ─── Dog Walk (2026-06-08) — single-writer off-by-one regression ─────────
#
# This trace is the one that exposed the mark_passes off-by-one: the
# stored passes were corrupted because a manual Start tap was mixed with
# the auto detector's own emits through two writers with incompatible
# index arithmetic. The manual-pass path has since been removed, leaving
# the auto detector as the SOLE writer. These tests pin the guarantee:
# fed the real trace, the detector emits a clean, contiguous,
# correctly-indexed 0..N-1 sequence — no shift, no dropped index.
# See sailline-docs/2026-06-08_session.md for the diagnosis.

_DOG_WALK_FIXTURE = (
    Path(__file__).parent / "fixtures" / "dog_walk_20260608.json"
)

# Course as stored on the race row (Start → 1 → 2 → 3 → Finish). Finish
# shares the Start coordinate — a real "return to the line" close.
_DOG_WALK_MARKS = [
    Mark(lat=41.93504, lon=-87.67391),                          # 0 Start
    Mark(lat=41.93510419063665, lon=-87.67758189483345),        # 1 Mark 1
    Mark(lat=41.93585925555956, lon=-87.67759868232766),        # 2 Mark 2
    Mark(lat=41.93588667813313, lon=-87.6742746154837),         # 3 Mark 3
    Mark(lat=41.93504, lon=-87.67391),                          # 4 Finish
]


def _load_dog_walk_points() -> list[Point]:
    """Load the Dog Walk trace. Same shape + tolerant ts parse as the
    Beer Can loader (studio export mixes Z and truncated fractions)."""
    import json as _json
    import re as _re

    raw = _json.loads(_DOG_WALK_FIXTURE.read_text())
    out: list[Point] = []
    for p in raw["points"]:
        ts_str = p["ts"].replace("Z", "+00:00")
        m = _re.match(r"(.+?)\.(\d+)([+-]\d{2}:\d{2})", ts_str)
        if m:
            frac = (m.group(2) + "000000")[:6]
            ts_str = f"{m.group(1)}.{frac}{m.group(3)}"
        out.append(
            Point(
                lat=float(p["lat"]),
                lon=float(p["lon"]),
                ts=datetime.fromisoformat(ts_str),
                speed_kts=float(p["sog_kts"]) if p.get("sog_kts") else 0.0,
            )
        )
    return out


@pytest.mark.skipif(
    not _DOG_WALK_FIXTURE.exists(),
    reason="Dog Walk fixture not checked in — see backend/tests/fixtures/README.md",
)
def test_dog_walk_auto_detection_is_contiguous_and_in_order():
    """The whole trace, inshore mode, single writer → exactly the five
    marks in order, indices 0..4 with no gap and no shift.

    Regression for the 2026-06-08 off-by-one: the stored data had the
    auto Start emit pushed to index 1 (a manual Start tap occupied 0),
    dropping a downstream index. With manual passes removed the detector
    is the only writer and MarkPass.mark_index == its position, so this
    sequence must stay contiguous."""
    points = _load_dog_walk_points()
    thresholds = thresholds_for_course(len(_DOG_WALK_MARKS), mode="inshore")
    passes = compute_passes(_DOG_WALK_MARKS, points, threshold_m=thresholds)
    assert [p.mark_index for p in passes] == [0, 1, 2, 3, 4]


@pytest.mark.skipif(
    not _DOG_WALK_FIXTURE.exists(),
    reason="Dog Walk fixture not checked in — see backend/tests/fixtures/README.md",
)
def test_dog_walk_passes_land_near_their_marks():
    """Each emitted pass's closest-approach point sits within the inshore
    capture distance of the mark it is indexed to — i.e. index N really
    is mark N, not a neighbour. Guards against a silent re-introduction
    of the index/position mismatch the off-by-one produced."""
    points = _load_dog_walk_points()
    thresholds = thresholds_for_course(len(_DOG_WALK_MARKS), mode="inshore")
    passes = compute_passes(_DOG_WALK_MARKS, points, threshold_m=thresholds)
    for p in passes:
        mark = _DOG_WALK_MARKS[p.mark_index]
        d = _haversine_check(p.lat, p.lon, mark.lat, mark.lon)
        assert d <= thresholds[p.mark_index], (
            f"pass {p.mark_index} CPA {d:.0f} m exceeds its mark's "
            f"threshold {thresholds[p.mark_index]:.0f} m — index/position "
            f"mismatch"
        )


def _haversine_check(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Local haversine for the assertion above (the detector's own is
    private; this keeps the test independent of its internals)."""
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))
