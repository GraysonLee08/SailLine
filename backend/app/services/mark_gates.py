"""Gate-based mark detection (v4) — lines and rays instead of circles.

Born from the 2026-07-01 Beer Can 7.1.2026 race, where the v3 CPA
detector modelled the start as a point + radius and missed a perfectly
normal start-line crossing ~390 m from the committee mark. Design
interview with the user (2026-07-02) settled the model:

  * **Start / finish** — a LINE through the start (finish) mark,
    perpendicular to the wind at gun time, extending
    ``LINE_HALF_LEN_M`` to each side (the RC sets the real pin an
    indeterminate distance out; a generous fixed length + the
    crossing-direction constraint is the practical stand-in for
    "indefinite"). Crossing the segment = passed. Direction-gated:
    a start counts only when crossing TOWARD the first leg, a finish
    only when crossing FROM the final leg — so sailing back over the
    line doesn't double-fire.
  * **Rounding / passage marks** — every mark carries a rounding side
    (``rounding: "port" | "starboard"`` in the marks JSONB — "leave
    mark to"). A RAY extends ``RAY_LEN_M`` from the mark on the side
    the boat passes: perpendicular to the inbound leg, to port of
    travel for "leave to starboard" (mark on the boat's right means
    the boat is on the mark's left) and to starboard of travel for
    "leave to port". The boat crosses the ray as it comes abeam.
    Fully automatic — no radius to be outside of.
  * **CPA fallback** — marks without a rounding side, courses without
    a resolvable line bearing, and any gate the geometry can't build
    fall back to the v3 CPA detector per mark. Old races keep working
    untouched.

Both detectors run in UNION for gated marks: a gate crossing emits
immediately (interpolated to the exact crossing instant); the CPA
path still emits if a GPS gap straddles the gate. Sequential
mark-ordering remains the false-positive guard, exactly as in v3.

Geometry is planar over a local equirectangular projection centred on
each gate's mark — at gate scales (≤ 2 km) the projection error is
centimetres, far below GPS noise.

Consumed by ``track_ingest.detect_and_persist_new_passes`` via
:class:`GateAwareDetector`, which persists both the CPA traversal
state AND the previous sample (needed for segment tests) in the same
``race_sessions.detector_state`` JSONB column (forward-compatible
keys — see migration 0020's notes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from app.services.mark_rounding import (
    Mark,
    MarkPass,
    MarkRoundingDetector,
    Point,
    _haversine_m,
)

# Start/finish line extends this far to EACH side of the mark (metres).
# 1 nm — RC lines are rarely longer; the crossing-direction constraint
# plus sequential ordering keeps a generous length safe.
LINE_HALF_LEN_M = 1852.0

# Rounding-mark ray length (metres). Long enough to catch wide passes
# past cribs/met buoys (the 2026-06-26 Silly Race saw a legitimate
# 342 m CPA), short enough not to snag an adjacent leg.
RAY_LEN_M = 1000.0

_EARTH_R_M = 6_371_000.0


# ─── Planar helpers ───────────────────────────────────────────────────


def _to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Equirectangular projection to metres, centred on (lat0, lon0)."""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * _EARTH_R_M
    y = math.radians(lat - lat0) * _EARTH_R_M
    return x, y


def _destination(
    lat: float, lon: float, bearing_deg: float, dist_m: float
) -> tuple[float, float]:
    """Flat-earth destination point — fine at gate scales (≤ 2 km)."""
    b = math.radians(bearing_deg)
    dlat = dist_m * math.cos(b) / _EARTH_R_M
    dlon = dist_m * math.sin(b) / (_EARTH_R_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def _bearing_deg(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def _cross(ox: float, oy: float, ax: float, ay: float, bx: float, by: float) -> float:
    """2D cross product of (a - o) × (b - o)."""
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def _segment_crossing_fraction(
    p1: tuple[float, float],
    p2: tuple[float, float],
    g1: tuple[float, float],
    g2: tuple[float, float],
) -> Optional[float]:
    """Fraction t ∈ [0, 1] along p1→p2 where it crosses segment g1→g2,
    or None if the segments don't intersect. Collinear overlap returns
    None — a track sliding exactly along a gate is not a crossing."""
    d1 = _cross(g1[0], g1[1], g2[0], g2[1], p1[0], p1[1])
    d2 = _cross(g1[0], g1[1], g2[0], g2[1], p2[0], p2[1])
    d3 = _cross(p1[0], p1[1], p2[0], p2[1], g1[0], g1[1])
    d4 = _cross(p1[0], p1[1], p2[0], p2[1], g2[0], g2[1])
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        denom = d1 - d2
        if denom == 0:
            return None
        return d1 / denom
    return None


# ─── Gate specification ───────────────────────────────────────────────


@dataclass(frozen=True)
class GateSpec:
    """How to detect a pass of one mark.

    kind:
      * ``"line"`` — segment ``a``→``b`` through the mark (start/finish).
        ``ref`` + ``ref_is_origin`` express the direction constraint:
        with ``ref_is_origin=True`` the crossing must START on ref's
        side (finish: coming from the last leg); with False it must
        END on ref's side (start: heading toward leg 1). ``ref=None``
        accepts either direction.
      * ``"ray"`` — segment from the mark (``a``) outward (``b``) on
        the side the boat passes. Any crossing counts.
      * ``"cpa"`` — no geometry; v3 CPA detection only.
    """
    kind: str  # "line" | "ray" | "cpa"
    mark_lat: float
    mark_lon: float
    a: Optional[tuple[float, float]] = None  # (lat, lon)
    b: Optional[tuple[float, float]] = None
    ref: Optional[tuple[float, float]] = None
    ref_is_origin: bool = False

    def crossing(
        self, p1: Point, p2: Point
    ) -> Optional[tuple[float, float, datetime]]:
        """Test the track segment p1→p2 against this gate.

        Returns ``(lat, lon, ts)`` interpolated at the crossing instant,
        or None. CPA gates always return None (handled elsewhere).
        """
        if self.kind == "cpa" or self.a is None or self.b is None:
            return None
        lat0, lon0 = self.mark_lat, self.mark_lon
        q1 = _to_xy(p1.lat, p1.lon, lat0, lon0)
        q2 = _to_xy(p2.lat, p2.lon, lat0, lon0)
        g1 = _to_xy(self.a[0], self.a[1], lat0, lon0)
        g2 = _to_xy(self.b[0], self.b[1], lat0, lon0)
        t = _segment_crossing_fraction(q1, q2, g1, g2)
        if t is None:
            return None
        if self.ref is not None:
            r = _to_xy(self.ref[0], self.ref[1], lat0, lon0)
            side_ref = _cross(g1[0], g1[1], g2[0], g2[1], r[0], r[1])
            side_p1 = _cross(g1[0], g1[1], g2[0], g2[1], q1[0], q1[1])
            side_p2 = _cross(g1[0], g1[1], g2[0], g2[1], q2[0], q2[1])
            if self.ref_is_origin:
                # Must depart FROM the ref side (finish line).
                if not (side_p1 * side_ref > 0 and side_p2 * side_ref <= 0):
                    return None
            else:
                # Must arrive ONTO the ref side (start line).
                if not (side_p2 * side_ref > 0 and side_p1 * side_ref <= 0):
                    return None
        lat = p1.lat + (p2.lat - p1.lat) * t
        lon = p1.lon + (p2.lon - p1.lon) * t
        dt = (p2.ts - p1.ts).total_seconds()
        ts = p1.ts + timedelta(seconds=dt * t)
        return lat, lon, ts


def _mark_pos(m: dict) -> Optional[tuple[float, float]]:
    try:
        return float(m["lat"]), float(m["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def build_gates(
    marks: list[dict],
    line_bearing_deg: Optional[float],
) -> list[GateSpec]:
    """Build one :class:`GateSpec` per mark from the JSONB mark dicts.

    * Index 0 (start) and index n-1 (finish, when n > 1): LINE through
      the mark along ``line_bearing_deg``, if a bearing is available.
      Start is direction-gated toward mark 1; finish is direction-gated
      from mark n-2.
    * Without a bearing, the START degrades to CPA. The FINISH degrades
      to a rounding RAY when it carries a ``rounding`` value (decision
      2026-07-02: ray + the always-on CPA union detects strictly more
      than CPA alone, and under-detecting the last mark is what wedged
      the 7.1 Beer Can pipeline), else CPA.
    * Intermediate marks: RAY on the passing side, when the mark has a
      valid ``rounding`` value — otherwise CPA.

    Never raises on malformed marks — any geometry failure degrades
    that mark to CPA, which is exactly the v3 behaviour.
    """
    n = len(marks)
    gates: list[GateSpec] = []
    for i, m in enumerate(marks):
        pos = _mark_pos(m)
        if pos is None:
            gates.append(GateSpec(kind="cpa", mark_lat=0.0, mark_lon=0.0))
            continue
        lat, lon = pos

        is_start = i == 0
        is_finish = i == n - 1 and n > 1
        if (is_start or is_finish) and line_bearing_deg is not None:
            a = _destination(lat, lon, line_bearing_deg % 360.0, LINE_HALF_LEN_M)
            b = _destination(
                lat, lon, (line_bearing_deg + 180.0) % 360.0, LINE_HALF_LEN_M
            )
            ref: Optional[tuple[float, float]] = None
            ref_is_origin = False
            if is_start and n > 1:
                ref = _mark_pos(marks[1])
                ref_is_origin = False  # must cross TOWARD leg 1
            elif is_finish and n > 1:
                ref = _mark_pos(marks[n - 2])
                ref_is_origin = True  # must cross FROM the last leg
            # Degenerate direction reference (e.g. next mark sits ON the
            # line) still works: side_ref == 0 fails both constraints,
            # so drop the ref rather than never detecting.
            if ref == (lat, lon):
                ref = None
            gates.append(
                GateSpec(
                    kind="line",
                    mark_lat=lat,
                    mark_lon=lon,
                    a=a,
                    b=b,
                    ref=ref,
                    ref_is_origin=ref_is_origin,
                )
            )
            continue

        rounding = m.get("rounding")
        if rounding in ("port", "starboard") and i > 0:
            prev = _mark_pos(marks[i - 1])
            if prev is not None and prev != pos:
                inbound = _bearing_deg(prev[0], prev[1], lat, lon)
                # "Leave mark to starboard" → mark on the boat's right →
                # the boat passes on the mark's LEFT relative to travel
                # (inbound − 90°). Port is the mirror.
                ray_dir = (
                    inbound - 90.0 if rounding == "starboard" else inbound + 90.0
                ) % 360.0
                b = _destination(lat, lon, ray_dir, RAY_LEN_M)
                gates.append(
                    GateSpec(
                        kind="ray",
                        mark_lat=lat,
                        mark_lon=lon,
                        a=(lat, lon),
                        b=b,
                    )
                )
                continue

        gates.append(GateSpec(kind="cpa", mark_lat=lat, mark_lon=lon))
    return gates


# ─── Detector ─────────────────────────────────────────────────────────


class GateAwareDetector:
    """v4 detector — gate crossings unioned with the v3 CPA fallback.

    Same external contract as :class:`MarkRoundingDetector`:
    ``feed_batch(points) -> list[MarkPass]``, ``dump_state()`` /
    constructor ``state=`` for cross-batch persistence, sequential
    mark ordering via ``next_mark_index``.

    State extends the v3 JSONB shape with the previous sample
    (``prev_lat``, ``prev_lon``, ``prev_ts``) so a gate crossing that
    straddles a batch boundary is still seen. v3 rows without those
    keys restore cleanly (first point of the next batch simply can't
    complete a segment — one sample of latency, once).
    """

    def __init__(
        self,
        marks: list[Mark],
        gates: list[GateSpec],
        threshold_m,
        next_mark_index: int = 0,
        state: Optional[dict] = None,
    ) -> None:
        if len(gates) != len(marks):
            raise ValueError("gates length must match marks length")
        self._marks = list(marks)
        self._gates = list(gates)
        self._cpa = MarkRoundingDetector(
            marks,
            threshold_m=threshold_m,
            next_mark_index=next_mark_index,
            state=state,
        )
        self._prev: Optional[Point] = None
        if state:
            plat = state.get("prev_lat")
            plon = state.get("prev_lon")
            pts = state.get("prev_ts")
            if (
                isinstance(plat, (int, float))
                and isinstance(plon, (int, float))
                and isinstance(pts, str)
            ):
                from app.services.mark_rounding import _parse_state_ts

                ts = _parse_state_ts(pts)
                if ts is not None:
                    self._prev = Point(lat=float(plat), lon=float(plon), ts=ts)

    @property
    def next_mark_index(self) -> int:
        return self._cpa.next_mark_index

    @property
    def done(self) -> bool:
        return self._cpa.done

    def _advance_past(self, emitted_index: int) -> None:
        """Keep the CPA sub-detector's index in lockstep after a gate
        emit (the gate saw the pass; CPA must stop watching that mark).
        MarkRoundingDetector has no public seek, but constructing its
        successor state is cheap and safe: fresh traversal at the next
        index."""
        if self._cpa.next_mark_index <= emitted_index:
            self._cpa = MarkRoundingDetector(
                self._marks,
                threshold_m=self._cpa._thresholds,  # noqa: SLF001 — same course, same thresholds
                next_mark_index=emitted_index + 1,
                state=None,
            )

    def feed_batch(self, points: Iterable[Point]) -> list[MarkPass]:
        out: list[MarkPass] = []
        for p in points:
            if self.done:
                break

            # 1. Gate test on the segment prev→p for the current target
            #    (and, chained, any immediately following targets — a
            #    single segment can legitimately cross the finish line
            #    right after rounding the last mark on a short course).
            while not self.done and self._prev is not None:
                idx = self._cpa.next_mark_index
                gate = self._gates[idx]
                hit = gate.crossing(self._prev, p)
                if hit is None:
                    break
                lat, lon, ts = hit
                out.append(MarkPass(mark_index=idx, ts=ts, lat=lat, lon=lon))
                self._advance_past(idx)

            # 2. CPA fallback for the current target (no-op when the
            #    gate just advanced — the fresh sub-detector starts a
            #    clean traversal on the new target with this point).
            if not self.done:
                r = self._cpa.feed(p)
                while r is not None:
                    out.append(r)
                    queued = getattr(self._cpa, "_queued_emit", None)
                    if queued is None:
                        r = None
                    else:
                        self._cpa._queued_emit = None  # noqa: SLF001
                        r = queued

            self._prev = p
        return out

    def dump_state(self) -> Optional[dict]:
        base = self._cpa.dump_state() or {}
        if self._prev is not None:
            base = dict(base)
            base["prev_lat"] = self._prev.lat
            base["prev_lon"] = self._prev.lon
            base["prev_ts"] = self._prev.ts.isoformat()
        return base or None
