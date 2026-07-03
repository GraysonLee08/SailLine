"""Section 1.0 — preprocessing for the post-race analysis metrics.

Turns raw ``track_points`` rows + the persisted ``wind_snapshot`` /
``obs_snapshot`` into a cleaned, wind-annotated sample list every other
race_analysis module consumes:

    AnalysisPoint:
        t          tz-aware datetime
        lat, lon   degrees
        sog_kts    5-sample rolling-median SOG
        cog_deg    5-sample rolling-median COG (circular median)
        twd_deg    blended wind-from at the boat (None = no coverage)
        tws_kts    blended wind speed at the boat (None = no coverage)
        twa_deg    signed TWA; negative = port tack (None = no wind)

Pure functions, no I/O — the worker loads rows and passes them in.

Wind sampling
-------------
``build_wind_sampler`` wraps the frozen ``wind_snapshot`` grid with
bilinear-in-space + **linear-in-time** interpolation (the snapshot's own
``snapshot_sampler`` is nearest-neighbour in time; the analysis spec
wants linear between the 15-min steps, so we sample the two bracketing
grid times and blend).

Buoy blending: when ``obs_snapshot`` has a station within ~10 nm, the
mean (buoy TWD − model TWD) offset over the race window is added to the
model TWD. Buoy anemometers sit at ~4–5 m vs the model's 10 m, so buoy
*speed* is only used for the gust column of the wind timeline, never to
correct model TWS (see observations/base.py's height-correction note).
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from app.services.race_analysis.geo import (
    angle_diff,
    circular_mean_deg,
    haversine_m,
    signed_twa_deg,
    uv_to_tws_twd,
)

# 1.0 filters.
MAX_GPS_ACC_M = 15.0
MAX_SOG_KT = 25.0

# Rolling-median window (samples) for SOG and COG smoothing.
MEDIAN_WINDOW = 5

# Buoy blending: only stations within this range influence TWD.
OBS_BLEND_MAX_KM = 18.52  # 10 nm

# Minimum paired (buoy, model) observations before we trust an offset.
_MIN_OBS_PAIRS = 3

# WindSampler: (lat, lon, t) -> (twd_deg, tws_kts) | None
WindAt = Callable[[float, float, datetime], Optional[tuple[float, float]]]


@dataclass
class AnalysisPoint:
    t: datetime
    lat: float
    lon: float
    sog_kts: Optional[float]
    cog_deg: Optional[float]
    twd_deg: Optional[float] = None
    tws_kts: Optional[float] = None
    twa_deg: Optional[float] = None


def _to_aware(value) -> Optional[datetime]:
    """str | datetime | None → tz-aware UTC datetime (same tolerant
    parse as ``performance._to_aware``)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _rolling_median(vals: list[Optional[float]], window: int) -> list[Optional[float]]:
    """Centered rolling median, ignoring Nones. None where no data."""
    half = window // 2
    out: list[Optional[float]] = []
    for i in range(len(vals)):
        lo = max(0, i - half)
        hi = min(len(vals), i + half + 1)
        win = sorted(v for v in vals[lo:hi] if v is not None)
        if not win:
            out.append(None)
            continue
        m = len(win)
        out.append(win[m // 2] if m % 2 else 0.5 * (win[m // 2 - 1] + win[m // 2]))
    return out


def _rolling_median_deg(vals: list[Optional[float]], window: int) -> list[Optional[float]]:
    """Circular rolling median for COG: unwrap each window around its
    center sample, take the ordinary median of the offsets, re-wrap.
    Robust across the 359°/1° seam."""
    half = window // 2
    out: list[Optional[float]] = []
    for i in range(len(vals)):
        ref = vals[i]
        lo = max(0, i - half)
        hi = min(len(vals), i + half + 1)
        if ref is None:
            # No center sample — fall back to any neighbour as reference.
            neighbours = [v for v in vals[lo:hi] if v is not None]
            if not neighbours:
                out.append(None)
                continue
            ref = neighbours[0]
        offsets = sorted(
            angle_diff(v, ref) for v in vals[lo:hi] if v is not None
        )
        if not offsets:
            out.append(None)
            continue
        m = len(offsets)
        med = offsets[m // 2] if m % 2 else 0.5 * (offsets[m // 2 - 1] + offsets[m // 2])
        out.append((ref + med) % 360.0)
    return out


# ─── Wind sampling over the frozen snapshot ───────────────────────────


def _parse_snapshot_grid(snapshot: dict) -> Optional[dict]:
    """Validate + parse the wind_snapshot dict once. None if unusable."""
    try:
        lats = [float(x) for x in snapshot["lats"]]
        lons = [float(x) for x in snapshot["lons"]]
        times = [_to_aware(t) for t in snapshot["times"]]
        u = snapshot["u_mps"]
        v = snapshot["v_mps"]
    except (KeyError, TypeError, ValueError):
        return None
    if not lats or not lons or not times or any(t is None for t in times):
        return None
    if len(u) != len(times) or len(v) != len(times):
        return None
    return {"lats": lats, "lons": lons, "times": times, "u": u, "v": v}


def _bilinear_uv(
    grid: dict, ti: int, lat: float, lon: float,
) -> Optional[tuple[float, float]]:
    """Bilinear (lat, lon) interpolation on one time slice, dropping
    null corners and renormalising — same null semantics as
    ``wind_snapshot.snapshot_sampler``."""
    lats, lons = grid["lats"], grid["lons"]
    u_sl, v_sl = grid["u"][ti], grid["v"][ti]

    def _bracket(vals: list[float], x: float) -> Optional[tuple[int, int, float]]:
        if x < vals[0] or x > vals[-1]:
            return None
        if len(vals) == 1:
            return 0, 0, 0.0
        j = min(bisect_right(vals, x), len(vals) - 1)
        i = max(j - 1, 0)
        if i == j:
            return i, j, 0.0
        span = vals[j] - vals[i]
        return i, j, (x - vals[i]) / span if span > 0 else 0.0

    bl = _bracket(lats, lat)
    bo = _bracket(lons, lon)
    if bl is None or bo is None:
        return None
    i0, i1, fa = bl
    j0, j1, fb = bo
    corners = (
        (i0, j0, (1 - fa) * (1 - fb)),
        (i0, j1, (1 - fa) * fb),
        (i1, j0, fa * (1 - fb)),
        (i1, j1, fa * fb),
    )
    u_acc = v_acc = w_acc = 0.0
    for ci, cj, w in corners:
        try:
            cu, cv = u_sl[ci][cj], v_sl[ci][cj]
        except (IndexError, TypeError):
            return None
        if cu is None or cv is None or w <= 0:
            continue
        u_acc += float(cu) * w
        v_acc += float(cv) * w
        w_acc += w
    if w_acc <= 0:
        return None
    return u_acc / w_acc, v_acc / w_acc


def _compute_obs_twd_offset(
    obs_snapshot: Optional[dict],
    model_uv_at: Callable[[float, float, datetime], Optional[tuple[float, float]]],
) -> tuple[float, str]:
    """Mean (buoy TWD − model TWD) over the race window for the nearest
    in-range station. Returns ``(offset_deg, source)`` where source is
    "blended" when an offset was applied, else "model"."""
    if not obs_snapshot:
        return 0.0, "model"
    stations = obs_snapshot.get("stations") or []
    in_range = [
        s for s in stations
        if isinstance(s.get("distance_km"), (int, float))
        and s["distance_km"] <= OBS_BLEND_MAX_KM
        and s.get("obs")
    ]
    if not in_range:
        return 0.0, "model"
    station = min(in_range, key=lambda s: s["distance_km"])
    lat, lon = station.get("lat"), station.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return 0.0, "model"
    diffs: list[float] = []
    for ob in station["obs"]:
        wdir = ob.get("wdir_deg")
        t = _to_aware(ob.get("ts"))
        if wdir is None or t is None:
            continue
        uv = model_uv_at(float(lat), float(lon), t)
        if uv is None:
            continue
        _, model_twd = uv_to_tws_twd(uv[0], uv[1])
        diffs.append(angle_diff(float(wdir), model_twd))
    if len(diffs) < _MIN_OBS_PAIRS:
        return 0.0, "model"
    mean = circular_mean_deg([d % 360.0 for d in diffs])
    if mean is None:
        return 0.0, "model"
    # circular_mean_deg returns [0, 360); fold back to signed.
    offset = ((mean + 180.0) % 360.0) - 180.0
    return offset, "blended"


def build_wind_sampler(
    wind_snapshot: Optional[dict],
    obs_snapshot: Optional[dict] = None,
) -> tuple[Optional[WindAt], str]:
    """Build ``(lat, lon, t) -> (twd_deg, tws_kts) | None`` over the
    frozen snapshot, linear in time, bilinear in space, with the buoy
    TWD offset applied. Returns ``(sampler, source)``; sampler is None
    when the snapshot is missing/unusable and source is one of
    "model" | "blended" | "none".
    """
    if not wind_snapshot:
        return None, "none"
    grid = _parse_snapshot_grid(wind_snapshot)
    if grid is None:
        return None, "none"
    times: list[datetime] = grid["times"]

    def _uv_at(lat: float, lon: float, t: datetime) -> Optional[tuple[float, float]]:
        if t < times[0] or t > times[-1]:
            return None
        j = min(bisect_right(times, t), len(times) - 1)
        i = max(j - 1, 0)
        uv0 = _bilinear_uv(grid, i, lat, lon)
        if i == j:
            return uv0
        uv1 = _bilinear_uv(grid, j, lat, lon)
        if uv0 is None:
            return uv1
        if uv1 is None:
            return uv0
        span = (times[j] - times[i]).total_seconds()
        f = (t - times[i]).total_seconds() / span if span > 0 else 0.0
        return (
            uv0[0] * (1 - f) + uv1[0] * f,
            uv0[1] * (1 - f) + uv1[1] * f,
        )

    offset_deg, source = _compute_obs_twd_offset(obs_snapshot, _uv_at)

    def _sampler(lat: float, lon: float, t: datetime) -> Optional[tuple[float, float]]:
        uv = _uv_at(lat, lon, t)
        if uv is None:
            return None
        tws, twd = uv_to_tws_twd(uv[0], uv[1])
        return (twd + offset_deg) % 360.0, tws

    return _sampler, source


# ─── Track cleaning ───────────────────────────────────────────────────


def clean_track(
    track_rows: list[dict],
    *,
    wind_at: Optional[WindAt] = None,
) -> list[AnalysisPoint]:
    """1.0 preprocessing: filter, smooth, annotate with wind + TWA.

    ``track_rows`` is the worker's row shape: ``{recorded_at, lat, lon,
    speed_kts, heading_deg[, gps_acc_m]}``. Rows failing the accuracy /
    SOG-sanity filters or missing position are dropped.
    """
    kept: list[dict] = []
    for r in track_rows:
        t = _to_aware(r.get("recorded_at"))
        lat, lon = r.get("lat"), r.get("lon")
        if t is None:
            continue
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
            continue
        acc = r.get("gps_acc_m")
        if isinstance(acc, (int, float)) and acc > MAX_GPS_ACC_M:
            continue
        sog = r.get("speed_kts")
        if isinstance(sog, (int, float)) and sog > MAX_SOG_KT:
            continue
        kept.append({
            "t": t, "lat": float(lat), "lon": float(lon),
            "sog": float(sog) if isinstance(sog, (int, float)) else None,
            "cog": (
                float(r["heading_deg"]) % 360.0
                if isinstance(r.get("heading_deg"), (int, float))
                and math.isfinite(float(r["heading_deg"]))
                else None
            ),
        })
    kept.sort(key=lambda p: p["t"])

    sogs = _rolling_median([p["sog"] for p in kept], MEDIAN_WINDOW)
    cogs = _rolling_median_deg([p["cog"] for p in kept], MEDIAN_WINDOW)

    out: list[AnalysisPoint] = []
    for p, sog, cog in zip(kept, sogs, cogs):
        pt = AnalysisPoint(
            t=p["t"], lat=p["lat"], lon=p["lon"],
            sog_kts=sog, cog_deg=cog,
        )
        if wind_at is not None:
            w = wind_at(pt.lat, pt.lon, pt.t)
            if w is not None:
                pt.twd_deg, pt.tws_kts = w
                if cog is not None:
                    pt.twa_deg = signed_twa_deg(pt.twd_deg, cog)
        out.append(pt)
    return out


def nearest_point(points: list[AnalysisPoint], t: datetime) -> Optional[AnalysisPoint]:
    """Nearest cleaned sample to ``t`` (points must be time-sorted)."""
    if not points:
        return None
    times = [p.t for p in points]
    j = min(bisect_right(times, t), len(points) - 1)
    i = max(j - 1, 0)
    return min((points[i], points[j]), key=lambda p: abs((p.t - t).total_seconds()))


def mean_sog_in_window(
    points: list[AnalysisPoint],
    t0: datetime,
    t1: datetime,
) -> Optional[float]:
    """Mean smoothed SOG over [t0, t1]. None when no samples fall in."""
    vals = [
        p.sog_kts for p in points
        if p.sog_kts is not None and t0 <= p.t <= t1
    ]
    return sum(vals) / len(vals) if vals else None


def distance_to_mark_m(pt: AnalysisPoint, mark: dict) -> Optional[float]:
    try:
        return haversine_m(pt.lat, pt.lon, float(mark["lat"]), float(mark["lon"]))
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "AnalysisPoint", "WindAt",
    "MAX_GPS_ACC_M", "MAX_SOG_KT", "MEDIAN_WINDOW", "OBS_BLEND_MAX_KM",
    "build_wind_sampler", "clean_track",
    "nearest_point", "mean_sog_in_window", "distance_to_mark_m",
]
