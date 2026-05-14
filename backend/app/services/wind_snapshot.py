"""Freeze a wind forecast over the race window into a stand-alone blob
that can live on the ``race_sessions.wind_snapshot`` JSONB column.

Why this exists
---------------
Live wind forecasts in Redis expire fast — HRRR after 2 h, GFS after
12 h (see ``workers/weather_ingest.py``). A user opening their stats
view the next morning would have no wind data to compare against their
track. Persisting a snapshot at race-end makes wind-vs-track analysis
permanent and decouples post-race analysis from the live ingest
pipeline.

What we store
-------------
A pre-sampled regular grid in (time, lat, lon) over the race's bbox +
window. Sampled values are stored as raw u/v components in m/s — the
same units the WindForecast returns — because:

  * The reader (AI prompt builder, frontend overlay) can derive dir
    and speed via atan2 / hypot exactly when it needs them, with no
    ambiguity at calm winds.
  * Storing two floats is simpler than storing four (u, v, dir,
    speed) and avoids consistency bugs if anyone updates one but not
    the other.

Schema::

    {
        "bbox":     [min_lat, min_lon, max_lat, max_lon],
        "grid_deg": 0.1,
        "lats":     [...],          # length M
        "lons":     [...],          # length N
        "t_start":  "2026-05-13T18:00:00Z",
        "t_end":    "2026-05-13T21:00:00Z",
        "dt_s":     900,
        "times":    ["2026-05-13T18:00:00Z", ...],  # length T
        "source":   "hybrid",       # WindForecast.quality
        "u_mps":    [[[...]]],      # shape (T, M, N)
        "v_mps":    [[[...]]],      # shape (T, M, N)
    }

Out-of-bounds samples (forecast doesn't cover that cell) and
out-of-window times are stored as ``null``. The reader treats null as
"no data here" rather than zero wind.

Size budget
-----------
A 3-hour buoy race over Chicago at 10 km grid is roughly 4×4 cells ×
13 time steps × 2 floats ≈ 416 numbers ≈ 10 KB JSON. A 30-hour Mac
race at 10 km grows to 4×4 × 120 × 2 ≈ 3 840 numbers ≈ 95 KB. Both
fit on the row without contortions; if we ever hit something larger
we'll add gzip+base64 compression here.

This module is pure-function over a WindForecast. No I/O. The Cloud
Run Job ``race-postprocess`` is responsible for loading the forecast
from the live ingest cache before calling in.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol


# ─── Defaults ──────────────────────────────────────────────────────────


# Resolution to sample the snapshot at when the caller doesn't pin
# one. 0.1° (~11 km) matches the HRRR-on-CONUS base ingest and is
# plenty for "did the wind shift during my race" analysis. Venues can
# pass 0.027° if they want native HRRR fidelity.
DEFAULT_GRID_DEG = 0.10

# Sample cadence in seconds. 900 s (15 min) is finer than the forecast
# itself (HRRR is hourly) but the linear interpolation in WindForecast
# gives us a smooth signal between cycle valid_times.
DEFAULT_DT_S = 900

# Pad the marks bbox by this many degrees in every direction so the
# snapshot covers the boat's actual track, not just the geodesic
# straight-line between marks.
DEFAULT_BBOX_PAD_DEG = 0.05  # ~5.5 km lat, less in lon at higher lat

# Hard caps so a runaway race doesn't blow the row size.
MAX_GRID_CELLS = 64       # M × N
MAX_TIME_STEPS = 200      # T


# ─── Inputs ────────────────────────────────────────────────────────────


class _ForecastLike(Protocol):
    """The bits of WindForecast we need. Declared as a protocol so the
    snapshotter can be tested with a fake — no need to stand up a real
    forecast with full grids."""
    quality: str

    def sample(
        self, lat: float, lon: float, valid_time: Optional[datetime] = None
    ) -> Optional[tuple[float, float]]: ...


@dataclass(frozen=True)
class MarkPoint:
    """One mark from a race's ``marks`` JSONB, for bbox derivation."""
    lat: float
    lon: float


# ─── Bbox helpers ──────────────────────────────────────────────────────


def marks_bbox(
    marks: list[dict], *, pad_deg: float = DEFAULT_BBOX_PAD_DEG,
) -> Optional[tuple[float, float, float, float]]:
    """Compute (min_lat, min_lon, max_lat, max_lon) over the marks
    with padding. Returns None if there are no usable marks."""
    pts: list[MarkPoint] = []
    for m in marks:
        try:
            pts.append(MarkPoint(lat=float(m["lat"]), lon=float(m["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not pts:
        return None
    min_lat = min(p.lat for p in pts) - pad_deg
    max_lat = max(p.lat for p in pts) + pad_deg
    min_lon = min(p.lon for p in pts) - pad_deg
    max_lon = max(p.lon for p in pts) + pad_deg
    return (min_lat, min_lon, max_lat, max_lon)


def _grid_axis(
    lo: float, hi: float, step: float, cap: int,
) -> list[float]:
    """Inclusive regular grid from ``lo`` to ``hi`` at ``step``,
    truncated to ``cap`` cells. Always emits at least two endpoints."""
    if hi <= lo:
        return [lo, lo + step]
    n = int(math.floor((hi - lo) / step)) + 1
    n = max(2, min(n, cap))
    return [lo + i * step for i in range(n)]


# ─── The snapshotter ──────────────────────────────────────────────────


def snapshot_forecast(
    forecast: _ForecastLike,
    *,
    bbox: tuple[float, float, float, float],
    t_start: datetime,
    t_end: datetime,
    grid_deg: float = DEFAULT_GRID_DEG,
    dt_s: int = DEFAULT_DT_S,
) -> dict:
    """Freeze ``forecast`` over (bbox, t_start..t_end) at the given
    resolution. Returns a JSONB-ready dict matching the schema in this
    module's docstring.

    Out-of-bounds and out-of-window samples are recorded as ``None``
    (null in JSON). The whole call is deterministic — same inputs
    produce the same output, which lets us regenerate the AI summary
    without worrying that the wind context drifted underneath it.
    """
    if t_end <= t_start:
        raise ValueError("t_end must be strictly after t_start")
    if grid_deg <= 0:
        raise ValueError("grid_deg must be positive")
    if dt_s <= 0:
        raise ValueError("dt_s must be positive")

    min_lat, min_lon, max_lat, max_lon = bbox

    # Spread the per-axis cell cap across lat and lon so very wide bboxes
    # don't blow MAX_GRID_CELLS on the lon axis alone. sqrt(cap) keeps
    # the cells roughly isotropic.
    per_axis_cap = max(2, int(math.sqrt(MAX_GRID_CELLS)))
    lats = _grid_axis(min_lat, max_lat, grid_deg, per_axis_cap)
    lons = _grid_axis(min_lon, max_lon, grid_deg, per_axis_cap)

    # Times: t_start, t_start+dt, ..., last <= t_end.
    span_s = (t_end - t_start).total_seconds()
    n_t = int(math.floor(span_s / dt_s)) + 1
    n_t = max(1, min(n_t, MAX_TIME_STEPS))
    times: list[datetime] = [
        t_start + timedelta(seconds=i * dt_s) for i in range(n_t)
    ]

    # Sample. Shape: (T, M, N).
    u_grid: list[list[list[Optional[float]]]] = []
    v_grid: list[list[list[Optional[float]]]] = []
    for t in times:
        u_slice: list[list[Optional[float]]] = []
        v_slice: list[list[Optional[float]]] = []
        for la in lats:
            u_row: list[Optional[float]] = []
            v_row: list[Optional[float]] = []
            for lo in lons:
                uv = forecast.sample(la, lo, t)
                if uv is None:
                    u_row.append(None)
                    v_row.append(None)
                else:
                    u_row.append(float(uv[0]))
                    v_row.append(float(uv[1]))
            u_slice.append(u_row)
            v_slice.append(v_row)
        u_grid.append(u_slice)
        v_grid.append(v_slice)

    return {
        "bbox": [min_lat, min_lon, max_lat, max_lon],
        "grid_deg": grid_deg,
        "lats": lats,
        "lons": lons,
        "t_start": _iso(t_start),
        "t_end": _iso(t_end),
        "dt_s": dt_s,
        "times": [_iso(t) for t in times],
        "source": getattr(forecast, "quality", "unknown"),
        "u_mps": u_grid,
        "v_mps": v_grid,
    }


# ─── Read-side helpers (used by the AI prompt builder and the FE) ────


def uv_to_dir_speed_kt(u_mps: float, v_mps: float) -> tuple[float, float]:
    """Convert (u, v) in m/s to (direction_from_deg, speed_kt).

    Direction is the meteorological convention: degrees-from-north of
    where the wind is COMING FROM. This is what sailors and the
    forecast display use.
    """
    speed_mps = math.hypot(u_mps, v_mps)
    speed_kt = speed_mps / 0.514444
    # Wind blowing TO (u, v) means it's coming FROM the opposite vector.
    # atan2(u, v) gives the bearing TO. Add 180° for FROM, wrap.
    dir_to = math.degrees(math.atan2(u_mps, v_mps))
    dir_from = (dir_to + 180.0) % 360.0
    return dir_from, speed_kt


def summarise_snapshot(snapshot: dict) -> dict:
    """Reduce a snapshot to a few headline numbers for prompt context.

    Returns ``{mean_speed_kt, max_speed_kt, mean_dir_deg, dir_range_deg,
    cell_coverage}``. ``cell_coverage`` is the fraction of grid cells
    with non-null data — useful in the prompt to warn "low forecast
    coverage" rather than ascribe meaning to a sparse signal.

    None inputs are skipped. If the snapshot is fully null, returns
    None for the numeric fields and 0.0 coverage.
    """
    u_grid = snapshot.get("u_mps") or []
    v_grid = snapshot.get("v_mps") or []
    speeds_kt: list[float] = []
    dirs_deg: list[float] = []
    total = 0
    filled = 0
    for u_slice, v_slice in zip(u_grid, v_grid):
        for u_row, v_row in zip(u_slice, v_slice):
            for u, v in zip(u_row, v_row):
                total += 1
                if u is None or v is None:
                    continue
                filled += 1
                d, s = uv_to_dir_speed_kt(u, v)
                speeds_kt.append(s)
                dirs_deg.append(d)
    coverage = (filled / total) if total else 0.0

    if not speeds_kt:
        return {
            "mean_speed_kt": None,
            "max_speed_kt": None,
            "mean_dir_deg": None,
            "dir_range_deg": None,
            "cell_coverage": coverage,
        }

    # Mean direction via vector mean (avoids the 359°/1° = 0° bug).
    sx = sum(math.cos(math.radians(d)) for d in dirs_deg)
    sy = sum(math.sin(math.radians(d)) for d in dirs_deg)
    mean_dir = (math.degrees(math.atan2(sy, sx)) + 360.0) % 360.0

    # Range: span around the mean. Cheap proxy for "did the wind shift
    # during the race." Computed as max angular deviation from the
    # mean.
    def _angdiff(a: float, b: float) -> float:
        d = (a - b + 540.0) % 360.0 - 180.0
        return abs(d)
    dir_range = max(_angdiff(d, mean_dir) for d in dirs_deg)

    return {
        "mean_speed_kt": sum(speeds_kt) / len(speeds_kt),
        "max_speed_kt": max(speeds_kt),
        "mean_dir_deg": mean_dir,
        "dir_range_deg": dir_range,
        "cell_coverage": coverage,
    }


# ─── Tiny helpers ──────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    """ISO-8601 with timezone info; assumes UTC if naive."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
