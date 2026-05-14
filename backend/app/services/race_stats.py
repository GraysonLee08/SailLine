"""Post-race stats — turn a raw GPS track + mark passes into the numbers
the stats view shows.

Pure function: no DB, no Redis, no network. Callers (the
``race-postprocess`` Cloud Run Job and the ``/api/races/{id}/stats``
endpoint) load the inputs and feed them in.

Inputs
------
* ``track_points`` — chronological list of ``TrackPoint`` (lat, lon,
  recorded_at, optional speed_kts). Mirrors the columns persisted in
  the ``track_points`` table (migration 0002). Speed_kts is the device
  Doppler speed when available; we prefer it over haversine-derived
  speed because GPS Doppler is more accurate at low speeds (~0.1 kt vs
  several-knot quantisation from coarse-time positional integration).
* ``marks`` — the race's ``marks`` JSONB, each entry needing at least
  ``{lat, lon}``. Extra fields (name, rounding) are passed through to
  leg labels.
* ``mark_passes`` — the race's ``mark_passes`` JSONB. Entries have
  ``{mark_index, ts, lat, lon}``. ``ts`` is an ISO string when read
  from the column; we tolerate datetimes too for in-memory tests.
* ``race_start_at`` — optional ``race_sessions.start_at``. When
  provided, leg 0 spans ``start_at → first pass``. When not provided,
  leg 0 spans ``first track point → first pass``.

Output
------
``RaceStats`` dataclass — see fields below. ``None`` is returned only
when the track is empty (the endpoint surfaces this as a 404).

Distance integration
--------------------
Haversine over consecutive points. **Distance is NOT integrated across
gaps longer than** ``GAP_THRESHOLD_S`` (default 30 s) — this handles
the screen-lock teleport problem we hit on the 2026-05-13 race, where
the recorder paused while the phone was locked and reported a 4 km
jump on resume. Time across the gap still counts toward elapsed (the
boat was sailing somewhere), but the unknown-distance segment doesn't
inflate the total.

Moving vs. stopped
------------------
Per-sample SOG is computed as the device ``speed_kts`` if present, or
``haversine_m / dt`` otherwise (capped at "no gap" to avoid teleport
inflation). A sample is "moving" if its SOG ≥ ``MOVING_THRESHOLD_KT``
(default 0.5 kt). Stopped time is everything else.

Leg splits
----------
One ``LegSplit`` per ``(prev_pass → next_pass)`` boundary. Leg 0 is
``start → mark_passes[0]``. The final leg is included only when all
marks have been rounded (``len(mark_passes) == len(marks)``) — DNF
shows the rounded legs without a misleading "finish" entry.

Speed series for the chart
--------------------------
Per-sample SOG over the whole race, downsampled with Douglas-Peucker
to at most ``MAX_SPEED_SAMPLES`` points (default 200). Epsilon is
``DP_EPSILON_KT`` (0.3 kt). The choice keeps shape — visible
acceleration around mark roundings, the long flat of a downwind leg —
while shrinking 1-Hz tracks (~7 k points over a 2 h race) by 30×+.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Iterable, Optional


# ─── Constants ─────────────────────────────────────────────────────────

# Earth radius in metres. Same constant the routing engine and the
# mark-rounding detector use; consistency matters more than the fourth
# decimal place.
_EARTH_R_M = 6_371_000.0

# Seconds threshold for "the recorder paused and reported a teleport".
# Calibrated against the 2026-05-13 Cook County track where iOS Safari
# pauses watchPosition for 10-90 s when the screen locks. 30 s is well
# under the shortest observed pause and well over a normal 1-2 s
# sample-to-sample gap.
GAP_THRESHOLD_S = 30.0

# Knots below which we count the boat as "stopped". Race timing
# convention is 0.5 kt; below this the GPS noise floor dominates.
MOVING_THRESHOLD_KT = 0.5

# m/s per knot.
_MPS_PER_KT = 0.514444

# Max points we return for the chart. 200 keeps the SVG sparkline
# light on the frontend and large enough to read mark-rounding spikes.
MAX_SPEED_SAMPLES = 200

# Douglas-Peucker tolerance for the speed series, in knots. Below this
# deviation a point is considered redundant. 0.3 kt is small relative
# to typical race-day variance (3-8 kt SOG swings on a sailboat).
DP_EPSILON_KT = 0.3


# ─── Input dataclasses ────────────────────────────────────────────────


@dataclass(frozen=True)
class TrackPoint:
    """One GPS sample. Mirrors the ``track_points`` row shape."""
    recorded_at: datetime
    lat: float
    lon: float
    speed_kts: Optional[float] = None
    heading_deg: Optional[float] = None


# ─── Output dataclasses ───────────────────────────────────────────────


@dataclass(frozen=True)
class LegSplit:
    """One leg of the race — boundary-to-boundary."""
    leg_index: int           # 0 = start → first mark
    from_label: str          # "Start" or "Mark N"
    to_label: str            # "Mark N" or "Finish"
    start_ts: datetime
    end_ts: datetime
    elapsed_s: float
    distance_m: float
    avg_sog_kt: float


@dataclass(frozen=True)
class SpeedSample:
    """One downsampled point on the speed-over-time chart."""
    t_offset_s: float        # seconds since the first track point
    sog_kt: float


@dataclass(frozen=True)
class RaceStats:
    """Everything the stats view renders, minus the AI summary."""
    point_count: int
    started_at: datetime
    ended_at: datetime
    elapsed_s: float
    moving_s: float
    stopped_s: float
    distance_m: float
    avg_sog_kt: float
    avg_moving_sog_kt: float
    max_sog_kt: float
    legs: list[LegSplit]
    speed_series: list[SpeedSample]

    def to_dict(self) -> dict:
        """JSON-ready dict for API responses and JSONB storage."""
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat()
        d["ended_at"] = self.ended_at.isoformat()
        for leg in d["legs"]:
            leg["start_ts"] = (
                leg["start_ts"].isoformat()
                if isinstance(leg["start_ts"], datetime)
                else leg["start_ts"]
            )
            leg["end_ts"] = (
                leg["end_ts"].isoformat()
                if isinstance(leg["end_ts"], datetime)
                else leg["end_ts"]
            )
        return d


# ─── Math helpers ──────────────────────────────────────────────────────


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Same formula as
    ``mark_rounding._haversine_m``; duplicated to keep this module
    importable from contexts that don't pull in the detector."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def _parse_ts(v) -> datetime:
    """Tolerant timestamp parser — accepts datetime or ISO string.

    JSONB reads back as a string; in-memory test fixtures pass
    datetime. Both go through this helper.
    """
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        # ``fromisoformat`` accepts ``+00:00`` but not ``Z``; normalise.
        s = v.replace("Z", "+00:00") if v.endswith("Z") else v
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise TypeError(f"unsupported timestamp type: {type(v)!r}")


# ─── Douglas-Peucker downsampler ──────────────────────────────────────


def _perpendicular_distance(
    t: float, v: float,
    t1: float, v1: float,
    t2: float, v2: float,
) -> float:
    """Perpendicular distance from (t, v) to the line through
    (t1, v1)-(t2, v2). Used by Douglas-Peucker on the speed series
    where ``t`` is seconds and ``v`` is knots.

    The "distance" is measured in knots — we treat the line as a
    function v(t) and compute |v - v_interp|, which is the natural
    fidelity measure for a chart that's plotted against time.
    Geometric perpendicular distance would mix the time and speed
    axes, which doesn't help here.
    """
    if t2 == t1:
        return abs(v - v1)
    # Linear interpolation along the chord.
    v_interp = v1 + (v2 - v1) * (t - t1) / (t2 - t1)
    return abs(v - v_interp)


def _douglas_peucker(
    points: list[tuple[float, float]],
    epsilon: float,
) -> list[int]:
    """Return the indices to keep after Douglas-Peucker simplification.

    Iterative implementation — recursion depth on a 10k-point series
    blows the default 1k Python limit.

    ``points`` is a list of ``(t_offset_s, sog_kt)`` tuples in time
    order. ``epsilon`` is the maximum vertical (knots) deviation
    allowed before a point is retained.
    """
    n = len(points)
    if n <= 2:
        return list(range(n))

    keep = [False] * n
    keep[0] = True
    keep[-1] = True

    # Stack of (start_idx, end_idx) ranges to inspect.
    stack: list[tuple[int, int]] = [(0, n - 1)]
    while stack:
        i_start, i_end = stack.pop()
        if i_end - i_start < 2:
            continue
        t1, v1 = points[i_start]
        t2, v2 = points[i_end]
        max_d = 0.0
        max_i = -1
        for i in range(i_start + 1, i_end):
            t, v = points[i]
            d = _perpendicular_distance(t, v, t1, v1, t2, v2)
            if d > max_d:
                max_d = d
                max_i = i
        if max_d > epsilon and max_i != -1:
            keep[max_i] = True
            stack.append((i_start, max_i))
            stack.append((max_i, i_end))

    return [i for i, k in enumerate(keep) if k]


def _downsample_speed_series(
    series: list[tuple[float, float]],
    *,
    epsilon: float = DP_EPSILON_KT,
    max_points: int = MAX_SPEED_SAMPLES,
) -> list[SpeedSample]:
    """Douglas-Peucker, then enforce ``max_points`` as a hard ceiling.

    If DP at the configured epsilon still leaves us with more than
    ``max_points`` points (very dynamic race), we re-run with a larger
    epsilon — doubling until we fit. Cheap binary-ish search; converges
    in <10 iterations on any realistic input.
    """
    if not series:
        return []
    if len(series) <= max_points:
        kept = _douglas_peucker(series, epsilon)
    else:
        eps = epsilon
        kept = _douglas_peucker(series, eps)
        while len(kept) > max_points:
            eps *= 2
            kept = _douglas_peucker(series, eps)
    return [SpeedSample(t_offset_s=series[i][0], sog_kt=series[i][1]) for i in kept]


# ─── The main computation ─────────────────────────────────────────────


def _segment_sog_kt(
    p_prev: TrackPoint, p_curr: TrackPoint, dist_m: float, dt_s: float,
) -> float:
    """SOG for the segment ending at ``p_curr``.

    Prefer the device-reported speed (Doppler — accurate). Fall back to
    haversine/dt only if the device returns null, which happens on
    desktops, sometimes on Android Chrome cold-start, and on any
    sample with ``speed_kts = None``.
    """
    if p_curr.speed_kts is not None and p_curr.speed_kts >= 0:
        return float(p_curr.speed_kts)
    if dt_s <= 0:
        return 0.0
    return (dist_m / dt_s) / _MPS_PER_KT


def compute_stats(
    track_points: list[TrackPoint],
    marks: list[dict],
    mark_passes: list[dict],
    race_start_at: Optional[datetime] = None,
) -> Optional[RaceStats]:
    """The whole pipeline. Returns ``None`` if there's no track to
    analyse."""
    if not track_points:
        return None

    pts = sorted(track_points, key=lambda p: p.recorded_at)
    started_at = pts[0].recorded_at
    ended_at = pts[-1].recorded_at
    elapsed_s = max(0.0, (ended_at - started_at).total_seconds())

    # Per-segment integration. First sample contributes time-to-itself
    # (0) and no distance — it has no predecessor.
    moving_s = 0.0
    stopped_s = 0.0
    distance_m = 0.0
    max_sog_kt = 0.0
    speed_series: list[tuple[float, float]] = []
    # First-sample SOG from the device if available.
    first_sog = (
        pts[0].speed_kts
        if pts[0].speed_kts is not None and pts[0].speed_kts >= 0
        else 0.0
    )
    speed_series.append((0.0, float(first_sog)))
    max_sog_kt = max(max_sog_kt, float(first_sog))

    for prev, curr in zip(pts, pts[1:]):
        dt_s = (curr.recorded_at - prev.recorded_at).total_seconds()
        if dt_s <= 0:
            # Duplicate or out-of-order timestamp — skip cleanly.
            continue
        # Distance integration skips gaps; time still counts.
        if dt_s <= GAP_THRESHOLD_S:
            seg_m = _haversine_m(prev.lat, prev.lon, curr.lat, curr.lon)
            distance_m += seg_m
        else:
            seg_m = 0.0
        sog_kt = _segment_sog_kt(prev, curr, seg_m, dt_s)
        max_sog_kt = max(max_sog_kt, sog_kt)
        if sog_kt >= MOVING_THRESHOLD_KT:
            moving_s += dt_s
        else:
            stopped_s += dt_s
        t_off = (curr.recorded_at - started_at).total_seconds()
        speed_series.append((t_off, sog_kt))

    avg_sog_kt = (
        (distance_m / elapsed_s) / _MPS_PER_KT if elapsed_s > 0 else 0.0
    )
    avg_moving_sog_kt = (
        (distance_m / moving_s) / _MPS_PER_KT if moving_s > 0 else 0.0
    )

    legs = _compute_legs(
        pts=pts,
        marks=marks,
        mark_passes=mark_passes,
        race_start_at=race_start_at,
    )
    speed_samples = _downsample_speed_series(speed_series)

    return RaceStats(
        point_count=len(pts),
        started_at=started_at,
        ended_at=ended_at,
        elapsed_s=elapsed_s,
        moving_s=moving_s,
        stopped_s=stopped_s,
        distance_m=distance_m,
        avg_sog_kt=avg_sog_kt,
        avg_moving_sog_kt=avg_moving_sog_kt,
        max_sog_kt=max_sog_kt,
        legs=legs,
        speed_series=speed_samples,
    )


def _compute_legs(
    *,
    pts: list[TrackPoint],
    marks: list[dict],
    mark_passes: list[dict],
    race_start_at: Optional[datetime],
) -> list[LegSplit]:
    """One LegSplit per (prev_boundary → next_pass).

    Boundaries: leg 0 starts at ``race_start_at`` (or the first track
    point if that's earlier), then each pass anchors the start of the
    next leg. We only emit legs that have both a start_ts and an
    end_ts on the actual track — a DNF race shows the legs the boat
    completed, not a phantom "finish" entry.
    """
    if not mark_passes:
        return []

    # Anchor of leg 0: race_start_at if set and not in the future
    # relative to the first track point; else first track point.
    first_track_ts = pts[0].recorded_at
    if race_start_at is None:
        leg0_start = first_track_ts
    else:
        # race_start_at might be naive in some test fixtures; normalise.
        rs = (
            race_start_at
            if race_start_at.tzinfo
            else race_start_at.replace(tzinfo=timezone.utc)
        )
        leg0_start = min(rs, first_track_ts) if rs <= first_track_ts else first_track_ts

    # All passes parsed to datetime.
    parsed_passes: list[dict] = []
    for p in mark_passes:
        parsed_passes.append(
            {
                "mark_index": int(p["mark_index"]),
                "ts": _parse_ts(p["ts"]),
                "lat": float(p["lat"]),
                "lon": float(p["lon"]),
            }
        )

    legs: list[LegSplit] = []
    prev_ts = leg0_start
    prev_label = "Start"
    for i, p in enumerate(parsed_passes):
        end_ts = p["ts"]
        if end_ts <= prev_ts:
            # Shouldn't happen, but if it does (clock skew, bad data)
            # skip the leg rather than emit a negative-elapsed entry.
            continue
        elapsed_s = (end_ts - prev_ts).total_seconds()
        # Per-leg distance: re-integrate haversine over the track
        # points that fall in [prev_ts, end_ts], using the same
        # gap-skipping rule.
        dist_m = _track_distance_in_window(pts, prev_ts, end_ts)
        avg_sog_kt = (
            (dist_m / elapsed_s) / _MPS_PER_KT if elapsed_s > 0 else 0.0
        )
        # Final pass = "Finish" only when all marks have been rounded.
        is_final = (
            i == len(parsed_passes) - 1
            and len(parsed_passes) == len(marks)
        )
        to_label = "Finish" if is_final else _mark_label(marks, p["mark_index"])
        legs.append(
            LegSplit(
                leg_index=i,
                from_label=prev_label,
                to_label=to_label,
                start_ts=prev_ts,
                end_ts=end_ts,
                elapsed_s=elapsed_s,
                distance_m=dist_m,
                avg_sog_kt=avg_sog_kt,
            )
        )
        prev_ts = end_ts
        prev_label = _mark_label(marks, p["mark_index"])

    return legs


def _mark_label(marks: list[dict], idx: int) -> str:
    """Human label for a mark index. Falls back to ``Mark N`` when the
    mark dict doesn't carry a name."""
    if 0 <= idx < len(marks):
        name = marks[idx].get("name") if isinstance(marks[idx], dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip()
    return f"Mark {idx + 1}"


def _track_distance_in_window(
    pts: list[TrackPoint], t_start: datetime, t_end: datetime,
) -> float:
    """Sum of haversine over consecutive points whose later endpoint
    falls in ``(t_start, t_end]``. Skips segments spanning gaps longer
    than ``GAP_THRESHOLD_S`` — same rule as the overall distance."""
    total = 0.0
    for prev, curr in zip(pts, pts[1:]):
        if curr.recorded_at <= t_start:
            continue
        if curr.recorded_at > t_end:
            break
        dt_s = (curr.recorded_at - prev.recorded_at).total_seconds()
        if 0 < dt_s <= GAP_THRESHOLD_S:
            total += _haversine_m(prev.lat, prev.lon, curr.lat, curr.lon)
    return total


# ─── Convenience for callers that have row dicts not dataclasses ──────


def track_points_from_rows(rows: Iterable[dict]) -> list[TrackPoint]:
    """Adapter: asyncpg rows → TrackPoint list. Keeps the router thin."""
    out: list[TrackPoint] = []
    for r in rows:
        out.append(
            TrackPoint(
                recorded_at=_parse_ts(r["recorded_at"]),
                lat=float(r["lat"]),
                lon=float(r["lon"]),
                speed_kts=(
                    float(r["speed_kts"]) if r.get("speed_kts") is not None else None
                ),
                heading_deg=(
                    float(r["heading_deg"]) if r.get("heading_deg") is not None else None
                ),
            )
        )
    return out
