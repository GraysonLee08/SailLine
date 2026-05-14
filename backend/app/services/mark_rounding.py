"""Mark-rounding detector — turns a stream of GPS points into a list of
"this mark was rounded at this instant" events.

Used by:
  * `tracks.py` POST handler — incrementally feeds each batch into the
    detector, appending new passes to ``race_sessions.mark_passes``.
  * The frontend's `useAutoStopRecorder` hook — its mirror in
    `frontend/src/lib/markRounding.js` runs the same algorithm on the
    in-memory point buffer for live UX (badge, ETA-to-auto-stop).
  * Session D's stats endpoint — leg splits derive from the ``ts`` of
    each pass.

Algorithm (v1, intentionally simple):
  * For mark ``i``, a "rounding" = the boat ENTERED the radius (a point
    inside the circle) AND then EXITED it (the next point outside). A
    fly-by where the boat enters and never leaves yet is *not* yet a
    rounding — wait for the exit.
  * Marks are detected strictly in order: mark ``i+1`` is not eligible
    until mark ``i`` has been rounded. Sailing crosses past a later mark
    on the way to an earlier one (common on W-L courses) shouldn't fire
    a false positive.
  * Default radius is 50 m. Big buoys + sloppy GPS may need more; making
    it configurable per-race is a follow-up.

What's deliberately NOT in v1:
  * No bearing-change check ("did the boat actually turn the corner?").
    Adds complexity; revisit only if false positives surface in real
    races.
  * No catch-up across many marks in a single batch — we re-evaluate
    state on every point. If the boat passed three marks in 30s, the
    detector finds all three.
  * No reverse-rounding (boat enters a later mark first and we backtrack
    earlier marks). Out of scope; tracks are recorded forward.

Distance math is haversine. Marks at sailing-relevant scales (tens of
metres) don't justify projecting to a local plane.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional


# Default radius for "inside the mark" — empirical: typical race buoys
# are ~1 m diameter, GPS error in good conditions is ~3-5 m, and the
# detector needs both an inside hit and an outside hit. 50 m is loose
# enough to catch realistic roundings without being so loose that a
# parallel pass triggers it.
DEFAULT_RADIUS_M = 50.0


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


class MarkRoundingDetector:
    """Stateful detector — feed points in chronological order, get passes.

    Resumable: callers (specifically the tracks router) construct the
    detector with the existing `mark_passes` from the DB, then feed only
    the new batch. The detector picks up at the right ``next_mark_index``
    and the right ``inside`` state.

    Resuming the ``inside`` flag is intentionally NOT done. The DB
    persists only the *completed* passes, not the in-progress entry
    state. On resume, ``inside`` defaults to False — meaning if the boat
    flushed a batch while INSIDE a mark's radius and then crossed out in
    the next batch, we'd miss the rounding. In practice the flush
    interval is 30 s and the radius is 50 m, so the boat would have to
    sit inside a mark across a flush, which doesn't happen in a race.
    Acceptable for v1; if it ever bites we can persist `inside` too.
    """

    def __init__(
        self,
        marks: list[Mark],
        radius_m: float = DEFAULT_RADIUS_M,
        next_mark_index: int = 0,
    ) -> None:
        if radius_m <= 0:
            raise ValueError("radius_m must be positive")
        if next_mark_index < 0:
            raise ValueError("next_mark_index must be >= 0")
        self._marks = list(marks)
        self._radius_m = float(radius_m)
        self._next = int(next_mark_index)
        self._inside = False  # are we currently inside the next mark's radius?

    @property
    def next_mark_index(self) -> int:
        """The index the detector is currently watching for. Equal to
        len(marks) means every mark has been rounded."""
        return self._next

    @property
    def done(self) -> bool:
        return self._next >= len(self._marks)

    def feed(self, point: Point) -> Optional[MarkPass]:
        """Consume one point. Returns a ``MarkPass`` if THIS point closed
        a rounding (i.e. we just exited the radius after having been
        inside), else None.

        State machine for the current target mark:
            outside -> inside  : record entry, no emit
            inside  -> inside  : no emit
            inside  -> outside : emit pass at THIS point's timestamp,
                                 advance ``next_mark_index``, reset state
            outside -> outside : no emit
        """
        if self.done:
            return None

        target = self._marks[self._next]
        d = _haversine_m(point.lat, point.lon, target.lat, target.lon)
        currently_inside = d <= self._radius_m

        emitted: Optional[MarkPass] = None
        if self._inside and not currently_inside:
            # Crossed out — that's a rounding.
            emitted = MarkPass(
                mark_index=self._next,
                ts=point.ts,
                lat=point.lat,
                lon=point.lon,
            )
            self._next += 1
            self._inside = False
            # If we just landed straight into the next mark's radius
            # (rare — would mean two marks within 50 m of each other),
            # re-evaluate on the same point so the entry is recorded.
            if not self.done:
                next_target = self._marks[self._next]
                d_next = _haversine_m(
                    point.lat, point.lon, next_target.lat, next_target.lon
                )
                if d_next <= self._radius_m:
                    self._inside = True
        else:
            self._inside = currently_inside

        return emitted

    def feed_batch(self, points: Iterable[Point]) -> list[MarkPass]:
        """Feed many points; collect all roundings produced."""
        out: list[MarkPass] = []
        for p in points:
            r = self.feed(p)
            if r is not None:
                out.append(r)
        return out


def compute_passes(
    marks: list[Mark],
    points: Iterable[Point],
    radius_m: float = DEFAULT_RADIUS_M,
) -> list[MarkPass]:
    """Convenience: full-track detection from scratch.

    Used by tests and by any caller that has the whole track in memory
    (e.g. a future "recompute mark passes" admin endpoint). The router
    uses the stateful class instead because it ingests incrementally.
    """
    det = MarkRoundingDetector(marks, radius_m=radius_m)
    return det.feed_batch(points)
