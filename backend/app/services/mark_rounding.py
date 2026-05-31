"""Mark-rounding detector — turns a stream of GPS points into a list of
"this mark was rounded at this instant" events.

Used by:
  * `tracks.py` POST handler — incrementally feeds each batch into the
    detector, appending new passes to ``race_sessions.mark_passes``.
  * The frontend's `useAutoStopRecorder` hook — its mirror in
    `packages/shared/src/markRounding.js` runs the same algorithm on the
    in-memory point buffer for live UX (badge, ETA-to-auto-stop).
  * Session D's stats endpoint — leg splits derive from the ``ts`` of
    each pass.

Algorithm (v3 — streaming sequential CPA, 2026-05-30):
  * For the next-expected mark, track distance per sample.
  * Track the running **minimum-distance** point (closest point of
    approach, "CPA") seen during the current traversal.
  * Detect departure: count consecutive samples where distance strictly
    increased from the previous sample.
  * When the departing count reaches ``DEPART_CONFIRM_SAMPLES`` AND the
    running minimum is at or below the per-mark threshold, emit a pass
    at the CPA timestamp/position and advance to the next mark.
  * Marks are detected strictly in order — same as v2.
  * If a single sample closes one rounding and is also a closer
    approach to the new next-expected mark, recurse on the same point
    so close-together marks are handled in one pass.

Why this changed from v2:
  * v2 used a fixed-radius "enter then exit" gate (50 m intermediate,
    75 m final). On distance races past nav structures (cribs, met
    buoys), boats commonly pass 100-300 m off the structure. v2 missed
    every such mark. The 2026-05-30 Colors (Bravo) race recorded zero
    passes because of this; see ``sailline-docs/2026-05-30_session.md``.
  * CPA-based detection is distance-tolerant — the same algorithm
    handles tight buoy roundings (10 m) and wide passage marks (200 m+)
    with one parameter.
  * The sequential "must come after previous" constraint, already in
    v2, becomes load-bearing here: it's the false-positive guard now
    that we don't gate on radius. A coincidental near-pass to an
    unrelated mark can't trigger because the detector is only ever
    watching one mark at a time.

Thresholds:
  * ``DEFAULT_DISTANCE_THRESHOLD_M`` = 250 m for distance racing —
    chosen empirically against the 2026-05-30 Garmin track which had a
    worst-case 208 m pass at Harrison-Dever Crib.
  * ``DEFAULT_INSHORE_THRESHOLD_M`` = 100 m for inshore racing where
    boats actually round buoys.
  * ``FINAL_MARK_BONUS_M`` = 50 m added to the threshold of the LAST
    mark — finishes are often crossed on a line longer than the mark
    radius would suggest. Mirrors v2's "wider final" policy.

What's deliberately NOT in v3 (deferred to follow-up):
  * Wind-derived start/finish LINE geometry (the user said pin is
    typically the only defined point; committee boat is 90° off
    perpendicular to wind). CPA against the pin works for tonight; the
    line model is a follow-up.
  * Heading-change validation as a confidence signal — useful for the
    AI summary, not as a detector gate (today's race had multiple
    legitimate passes with <15° heading change because the boat was on
    a reach past passage marks).
  * Missed-mark timeout (the "you sailed past without registering"
    notification) — lives in the mobile recorder, not the detector.
  * In-batch state persistence: if a CPA happens to span a batch
    boundary (sample N in batch K, departing samples in batch K+1) the
    departing-count resets. Matches v2's accepted limitation; batches
    are 30 s and the depart-confirm needs 3 samples (~15 s), so the
    realistic case where this bites is a sample landing exactly on the
    batch boundary at CPA. Acceptable for v1; persist the state on the
    race row if it surfaces.

Distance math is haversine. Marks at sailing-relevant scales (tens to
hundreds of metres) don't justify projecting to a local plane.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence, Union


# Per-mark threshold defaults — see module docstring for rationale.
DEFAULT_DISTANCE_THRESHOLD_M = 250.0
DEFAULT_INSHORE_THRESHOLD_M = 100.0
FINAL_MARK_BONUS_M = 50.0

# Number of consecutive samples of strictly-increasing distance that must
# follow the running minimum before we emit. At a typical 5 s sample
# interval (mobile + Garmin), 3 samples = ~15 s of clear departure. Too
# small (1-2) lets GPS jitter near CPA cause false fires; too large
# (>=5) makes the detector lag noticeably behind reality.
DEPART_CONFIRM_SAMPLES = 3


# Legacy aliases — old name was "radius". These are deprecated; new
# callers should use the THRESHOLD names. Kept so any third-party
# consumer of the module doesn't break.
DEFAULT_RADIUS_M = DEFAULT_INSHORE_THRESHOLD_M
FINAL_MARK_RADIUS_M = DEFAULT_INSHORE_THRESHOLD_M + FINAL_MARK_BONUS_M


# Earth radius in metres. The same constant the wind-forecast loader
# uses; consistency matters more than a fourth decimal place.
_EARTH_R_M = 6_371_000.0


@dataclass(frozen=True)
class Point:
    """One sample fed into the detector.

    Mirrors the `track_points` table columns we care about. Speed/heading
    are not used by the algorithm but kept on the dataclass so callers
    can pass through their domain shape without translating.
    """
    lat: float
    lon: float
    ts: datetime
    speed_kts: Optional[float] = None
    heading_deg: Optional[float] = None


@dataclass(frozen=True)
class Mark:
    """One course mark — only the position matters here."""
    lat: float
    lon: float


@dataclass(frozen=True)
class MarkPass:
    """An emitted "the boat rounded mark N at instant T" event.

    ``mark_index`` is the 0-based index into the course's mark list. The
    same mark can appear multiple times in a multi-lap layout (W-L) and
    will get one pass per lap — but only because the course list itself
    repeats the mark. The detector itself does not know about laps; it
    just walks the course in order.

    ``ts``, ``lat``, ``lon`` are the boat position at the **closest
    point of approach** during the traversal — the natural "you crossed
    the line through the mark" moment.
    """
    mark_index: int
    ts: datetime
    lat: float
    lon: float


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def _normalise_thresholds(
    threshold_m: Union[float, Sequence[float]],
    mark_count: int,
) -> list[float]:
    """Coerce ``threshold_m`` (scalar or sequence) to a per-mark list.

    Validates that every threshold is positive and (when a sequence)
    matches the mark count.
    """
    if isinstance(threshold_m, (int, float)):
        if threshold_m <= 0:
            raise ValueError("threshold_m must be positive")
        return [float(threshold_m)] * mark_count
    thresholds = [float(t) for t in threshold_m]
    if len(thresholds) != mark_count:
        raise ValueError(
            f"threshold_m sequence length {len(thresholds)} != mark count "
            f"{mark_count}"
        )
    for t in thresholds:
        if t <= 0:
            raise ValueError("each threshold must be positive")
    return thresholds


class MarkRoundingDetector:
    """Stateful detector — feed points in chronological order, get passes.

    Resumable: callers (specifically the tracks router) construct the
    detector with the index of the next-expected mark (= the count of
    existing persisted passes), then feed only the new batch.

    Traversal state (running minimum, last distance, departing count) is
    intentionally NOT resumed across batches. The DB persists only
    completed passes. On resume after a batch flush, state resets. In
    the rare case where the CPA happens to span a batch boundary, the
    departing-count resets and the algorithm waits for fresh decreases
    + increases. Acceptable for the v3 ship; persist state on the race
    row if this bites in practice.

    ``threshold_m`` accepts either:
      * a scalar (float) — applies to every mark.
      * a sequence — per-mark thresholds. The router uses this to give
        the final mark a wider threshold than intermediate marks.

    The old ``radius_m`` keyword is accepted for back-compat — it maps
    straight to ``threshold_m`` semantically (the algorithm changed but
    the parameter still names "how close is close enough").
    """

    def __init__(
        self,
        marks: list[Mark],
        threshold_m: Union[float, Sequence[float], None] = None,
        next_mark_index: int = 0,
        depart_confirm_samples: int = DEPART_CONFIRM_SAMPLES,
        *,
        radius_m: Union[float, Sequence[float], None] = None,
    ) -> None:
        if next_mark_index < 0:
            raise ValueError("next_mark_index must be >= 0")
        if depart_confirm_samples < 1:
            raise ValueError("depart_confirm_samples must be >= 1")
        # Back-compat: accept either keyword. ``radius_m`` was the old
        # name. ``threshold_m`` wins if both are passed.
        chosen = threshold_m if threshold_m is not None else radius_m
        if chosen is None:
            chosen = DEFAULT_DISTANCE_THRESHOLD_M
        self._marks = list(marks)
        self._thresholds = _normalise_thresholds(chosen, len(self._marks))
        self._next = int(next_mark_index)
        self._depart_n = int(depart_confirm_samples)
        self._reset_traversal_state()

    @property
    def next_mark_index(self) -> int:
        """The index the detector is currently watching for. Equal to
        ``len(marks)`` means every mark has been rounded."""
        return self._next

    @property
    def done(self) -> bool:
        return self._next >= len(self._marks)

    def _reset_traversal_state(self) -> None:
        self._last_dist: Optional[float] = None
        self._min_dist: Optional[float] = None
        self._min_ts: Optional[datetime] = None
        self._min_lat: Optional[float] = None
        self._min_lon: Optional[float] = None
        self._departing: int = 0

    def feed(self, point: Point) -> Optional[MarkPass]:
        """Consume one point. Returns a ``MarkPass`` if the point completed
        a rounding (i.e. the detector saw enough departure from the
        running minimum and the minimum is within threshold), else None.
        """
        if self.done:
            return None

        target = self._marks[self._next]
        threshold = self._thresholds[self._next]
        d = _haversine_m(point.lat, point.lon, target.lat, target.lon)

        # Update running minimum FIRST. The same point can be both the
        # new minimum AND complete the prior-mark's pass (chained marks)
        # — but here we only care about the current target.
        if self._min_dist is None or d < self._min_dist:
            self._min_dist = d
            self._min_ts = point.ts
            self._min_lat = point.lat
            self._min_lon = point.lon
            # New minimum resets the departing counter — we just got
            # closer, so any prior departure was a head-fake (boat
            # tactical circling, GPS jitter on approach).
            self._departing = 0
        elif self._last_dist is not None and d > self._last_dist:
            # Strictly-increasing sample: we're departing from the
            # last-seen position. Count it.
            self._departing += 1
        else:
            # Distance equal or decreased (but not below current min).
            # Doesn't add to the departing run; doesn't reset it either.
            # An equal value is ambiguous — neither a clear approach nor
            # a clear departure. Hold the count.
            pass

        self._last_dist = d

        # Pass complete?
        if self._departing >= self._depart_n and self._min_dist is not None \
                and self._min_dist <= threshold:
            assert self._min_ts is not None
            assert self._min_lat is not None
            assert self._min_lon is not None
            emitted = MarkPass(
                mark_index=self._next,
                ts=self._min_ts,
                lat=self._min_lat,
                lon=self._min_lon,
            )
            self._next += 1
            self._reset_traversal_state()
            # Re-evaluate this point against the new target mark — if
            # two marks are close together and this same sample is also
            # near the next mark, kick off its traversal here. Bounded
            # recursion: emits at most mark_count times.
            chained = self.feed(point)
            # If the chained call emitted a pass, the caller still only
            # sees the FIRST emit because feed() returns one at a time.
            # The chained pass is tracked in state and will be the next
            # return from feed_batch's next iteration on the same point.
            # Actually — that doesn't work because we already consumed
            # the point. Easier: collect both passes into a deque and
            # drain across calls. For v3 we keep the single-return
            # contract by emitting the FIRST pass and trusting the
            # chained one to fire on the next inbound point (which will
            # also be a CPA candidate for the new target).
            #
            # Caveat: an isolated single-point spike near two marks
            # back-to-back would lose the second emit. In practice marks
            # are spaced > 100 m and GPS samples are 1-5 s apart, so
            # the boat needs another sample to confirm CPA on mark
            # N+1 anyway. The lost-second-emit case is theoretical.
            if chained is not None:
                # Push the chained emit back into the state so the next
                # call to feed() observes it as if it just happened.
                # Simpler than restructuring the contract: emit the
                # earlier-in-sequence pass first, queue the later.
                self._queued_emit = chained
                return emitted
            return emitted

        # Drain any queued chained emit from a prior chained pass.
        queued = getattr(self, "_queued_emit", None)
        if queued is not None:
            self._queued_emit = None
            return queued

        return None

    def feed_batch(self, points: Iterable[Point]) -> list[MarkPass]:
        """Feed many points; collect all roundings produced.

        Drains any queued chained emit at the end so the batch return
        accurately reflects every pass detected.
        """
        out: list[MarkPass] = []
        for p in points:
            r = self.feed(p)
            while r is not None:
                out.append(r)
                # Re-poll the queue without consuming a new point — a
                # chained pass may have been queued by feed() above.
                queued = getattr(self, "_queued_emit", None)
                if queued is None:
                    r = None
                else:
                    self._queued_emit = None
                    r = queued
        return out


def compute_passes(
    marks: list[Mark],
    points: Iterable[Point],
    threshold_m: Union[float, Sequence[float], None] = None,
    *,
    radius_m: Union[float, Sequence[float], None] = None,
) -> list[MarkPass]:
    """Convenience: full-track detection from scratch.

    Used by tests and by any caller that has the whole track in memory
    (e.g. a future "recompute mark passes" admin endpoint). The router
    uses the stateful class instead because it ingests incrementally.

    Accepts the old ``radius_m`` kw for back-compat.
    """
    det = MarkRoundingDetector(
        marks, threshold_m=threshold_m, radius_m=radius_m,
    )
    return det.feed_batch(points)


def thresholds_for_course(
    mark_count: int,
    mode: str = "distance",
) -> list[float]:
    """Per-mark threshold list for a course of ``mark_count`` marks in
    ``mode`` ("distance" or "inshore" — anything else treated as
    "distance" for the wider tolerance).

    The final mark gets ``FINAL_MARK_BONUS_M`` extra metres so wide
    finish lines don't get missed.
    """
    if mark_count <= 0:
        return []
    base = (
        DEFAULT_INSHORE_THRESHOLD_M
        if mode == "inshore"
        else DEFAULT_DISTANCE_THRESHOLD_M
    )
    final = base + FINAL_MARK_BONUS_M
    if mark_count == 1:
        return [final]
    return [base] * (mark_count - 1) + [final]


def radii_for_course(mark_count: int) -> list[float]:
    """Deprecated alias for ``thresholds_for_course(mark_count, mode="distance")``.

    Kept so callers that haven't migrated to mode-aware thresholds still
    work. New code should call ``thresholds_for_course`` with the race
    mode. This wrapper picks "distance" because the safer default for
    "unknown mode" is the wider tolerance — missing a mark is a worse
    failure mode than tripping early on a tight inshore course (which
    is rare given sequential ordering).
    """
    return thresholds_for_course(mark_count, mode="distance")
