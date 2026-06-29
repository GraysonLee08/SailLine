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
  * ``DEFAULT_DISTANCE_THRESHOLD_M`` = 400 m for distance racing —
    bumped from 250 m after the 2026-06-26 Silly Race where the phone's
    GPS CPA to Mark 1 (4 Mile Crib) was 342 m — outside the old 250 m
    threshold but a legitimate rounding distance for a crib mark on
    a distance race. The Garmin track showed a 62 m CPA, confirming
    the mark was actually rounded; the phone's worse GPS fix during a
    telemetry gap caused the miss.
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

Cross-batch state persistence (added 2026-06-04):

The v3 docstring previously called batch-boundary state loss "acceptable
for v1." The 2026-06-03 Beer Can Race 4 proved otherwise: with the
mobile native uploader running ``autoSyncThreshold: 1``, batches arrive
containing 1-3 samples each. The depart-confirm window (3 increasing
samples after CPA) spans 3-4 batches in that cadence, and ``_last_dist``
resets to ``None`` at the start of every batch — so the increment branch
``elif self._last_dist is not None and d > self._last_dist`` never
fires. The detector misses the pass despite a clean 1.1 m CPA.

The detector now exposes :meth:`dump_state` and :meth:`restore_state`
so callers can persist the traversal state across batches. ``track_ingest``
writes the dumped state to ``race_sessions.detector_state`` (added in
migration 0020) after every batch and restores it on the next call.
NULL state means "fresh traversal" — matches the v3 behaviour for the
first batch of a race, and the behaviour immediately after a pass emits
(``_reset_traversal_state`` is called inside ``feed``).

Distance math is haversine. Marks at sailing-relevant scales (tens to
hundreds of metres) don't justify projecting to a local plane.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence, Union


# Per-mark threshold defaults — see module docstring for rationale.
DEFAULT_DISTANCE_THRESHOLD_M = 400.0
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


def _opt_float(v) -> Optional[float]:
    """Coerce a JSONB value to float-or-None.

    JSONB-decoded values reach us as ``int | float | None``. asyncpg's
    global JSONB codec normalises numeric types but we still see ``int``
    for whole-number values (e.g. ``departing: 3`` round-trips as int).
    """
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_state_ts(s: str) -> Optional[datetime]:
    """Parse the ISO-8601 ``min_ts`` produced by :meth:`dump_state`.

    Tolerant of the trailing ``Z`` shape some serialisers prefer; the
    in-house dumper always uses ``+00:00`` but a hand-written test
    fixture or a future client that round-trips through JSON.dump could
    well land here with Z.
    """
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


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

    Resumable on two axes:

    1. **Mark index** — construct with ``next_mark_index`` equal to the
       count of already-persisted passes. The detector skips marks the
       race has already rounded.

    2. **Traversal state** — for streaming uploaders that emit batches
       too small to contain the full CPA + depart pattern, callers can
       persist the running ``{last_dist, min_dist, min_ts, min_lat,
       min_lon, departing}`` from :meth:`dump_state` and restore it on
       the next batch via :meth:`restore_state`. Without persistence
       the depart-confirm counter would reset on every batch and the
       detector would never emit when uploads run at ~1 sample/batch
       (the realistic mobile cadence under good connectivity).

    Constructing with ``state=None`` (the default) gives a fresh
    traversal — equivalent to the pre-2026-06-04 behaviour for the
    very first batch of a race or immediately after a pass emits.

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
        state: Optional[dict] = None,
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
        # Restore last-batch traversal state if the caller has it. Safe
        # to call with None — no-op. Safe to call with a stale state
        # for an already-rounded mark — the detector validates ``done``
        # at the top of ``feed`` and ignores the state in that case.
        if state is not None:
            self.restore_state(state)

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

    # ── State persistence (added 2026-06-04 for cross-batch detection) ──

    def dump_state(self) -> Optional[dict]:
        """Return a JSONB-friendly snapshot of the current traversal state,
        or ``None`` if no traversal is in progress (fresh / just-reset).

        Pair with :meth:`restore_state` on the next-batch detector.
        Caller persists the dict to ``race_sessions.detector_state`` and
        passes it back into the next constructor.

        Returns ``None`` (not an empty dict) when there is nothing to
        persist, so the caller can write SQL NULL — keeps the JSONB
        column clean for races that haven't fed a sample yet, and for
        races where the previous batch ended exactly on a pass emit
        (which calls ``_reset_traversal_state``).

        ``next_mark_index`` is NOT part of the state — it is derived
        from ``len(existing_passes)`` by the caller, so persisting it
        here would duplicate truth.
        """
        if (
            self._last_dist is None
            and self._min_dist is None
            and self._departing == 0
        ):
            return None
        return {
            "last_dist": self._last_dist,
            "min_dist": self._min_dist,
            "min_ts": self._min_ts.isoformat() if self._min_ts else None,
            "min_lat": self._min_lat,
            "min_lon": self._min_lon,
            "departing": self._departing,
        }

    def restore_state(self, state: Optional[dict]) -> None:
        """Restore a traversal state previously produced by
        :meth:`dump_state`.

        ``None`` is a no-op (caller hasn't seen a state yet). An empty
        dict ``{}`` is treated the same way. Any missing key falls back
        to the reset-state default so a partial / older-shape persisted
        state remains usable across detector upgrades.
        """
        if not state:
            return
        self._last_dist = _opt_float(state.get("last_dist"))
        self._min_dist = _opt_float(state.get("min_dist"))
        ts_raw = state.get("min_ts")
        if isinstance(ts_raw, str):
            self._min_ts = _parse_state_ts(ts_raw)
        elif isinstance(ts_raw, datetime):
            self._min_ts = ts_raw
        else:
            self._min_ts = None
        self._min_lat = _opt_float(state.get("min_lat"))
        self._min_lon = _opt_float(state.get("min_lon"))
        dep = state.get("departing", 0)
        try:
            self._departing = int(dep) if dep is not None else 0
        except (TypeError, ValueError):
            self._departing = 0

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
