"""Section 1.8 — wind timeline and events.

One row per 5 min (downsampled to 15 min when the race would produce
more than _MAX_ROWS rows — long distance races): TWD, TWS at the boat's
position, gust from the nearest obs station, source tag.

Events, detected on the TWD/TWS series:
* ``persistent_shift`` — TWD moves ≥ SHIFT_MIN_DEG within 15 min and
  does NOT return within the following 15 min.
* ``oscillation``      — same move, returns within 15 min.
* ``building`` / ``dying`` — TWS trend over the race exceeds
  TREND_MIN_KT total change with a consistent sign.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.services.race_analysis.geo import angle_diff
from app.services.race_analysis.preprocess import (
    AnalysisPoint,
    WindAt,
    nearest_point,
)

ROW_INTERVAL_S = 300
COARSE_INTERVAL_S = 900
_MAX_ROWS = 60

SHIFT_MIN_DEG = 10.0
SHIFT_WINDOW_S = 900.0     # "within 15 min"
RETURN_WINDOW_S = 900.0    # oscillation must return within 15 min
_RETURN_TOL_DEG = 5.0

TREND_MIN_KT = 3.0

# Gust lookup: nearest obs within this time window of the row.
_GUST_MATCH_S = 450.0


def _to_aware(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _gust_series(obs_snapshot: Optional[dict]) -> list[tuple[datetime, float]]:
    """(ts, gust_kts) from the nearest station that reports gusts."""
    if not obs_snapshot:
        return []
    stations = sorted(
        (s for s in obs_snapshot.get("stations") or [] if s.get("obs")),
        key=lambda s: s.get("distance_km") or 1e9,
    )
    for st in stations:
        out: list[tuple[datetime, float]] = []
        for ob in st["obs"]:
            t = _to_aware(ob.get("ts"))
            g = ob.get("gst_mps")
            if t is None or not isinstance(g, (int, float)):
                continue
            out.append((t, float(g) * 1.94384))
        if out:
            out.sort(key=lambda x: x[0])
            return out
    return []


def build_wind_timeline(
    points: list[AnalysisPoint],
    *,
    wind_at: Optional[WindAt],
    obs_snapshot: Optional[dict],
    source: str,
    t_start: datetime,
    t_end: datetime,
) -> tuple[list[dict], list[dict]]:
    """Returns ``(rows, events)``. Empty when there's no wind sampler."""
    if wind_at is None or t_end <= t_start:
        return [], []

    span_s = (t_end - t_start).total_seconds()
    interval = ROW_INTERVAL_S
    if span_s / interval > _MAX_ROWS:
        interval = COARSE_INTERVAL_S

    gusts = _gust_series(obs_snapshot)

    rows: list[dict] = []
    t = t_start
    while t <= t_end:
        p = nearest_point(points, t)
        if p is None:
            t += timedelta(seconds=interval)
            continue
        w = wind_at(p.lat, p.lon, t)
        if w is not None:
            twd, tws = w
            gust = _nearest_gust(gusts, t)
            rows.append({
                "t": t.isoformat(timespec="seconds"),
                "twd_deg": round(twd, 1),
                "tws_kts": round(tws, 1),
                "gust_kts": round(gust, 1) if gust is not None else None,
                "source": source,
            })
        t += timedelta(seconds=interval)

    events = _detect_events(rows)
    return rows, events


def _nearest_gust(
    gusts: list[tuple[datetime, float]], t: datetime,
) -> Optional[float]:
    best: Optional[float] = None
    best_dt = _GUST_MATCH_S
    for ts, g in gusts:
        dt = abs((ts - t).total_seconds())
        if dt < best_dt:
            best, best_dt = g, dt
    return best


def _detect_events(rows: list[dict]) -> list[dict]:
    if len(rows) < 3:
        return []
    parsed = [
        (_to_aware(r["t"]), r["twd_deg"], r["tws_kts"]) for r in rows
    ]
    events: list[dict] = []

    # ── Shifts: compare each row to rows ≤ 15 min earlier. ────────────
    claimed_until: Optional[datetime] = None
    for i, (t_i, twd_i, _) in enumerate(parsed):
        if claimed_until is not None and t_i <= claimed_until:
            continue
        for j in range(i - 1, -1, -1):
            t_j, twd_j, _ = parsed[j]
            if (t_i - t_j).total_seconds() > SHIFT_WINDOW_S:
                break
            move = angle_diff(twd_i, twd_j)
            if abs(move) < SHIFT_MIN_DEG:
                continue
            # Did it return toward twd_j within RETURN_WINDOW_S?
            returned = False
            for t_k, twd_k, _ in parsed[i + 1:]:
                if (t_k - t_i).total_seconds() > RETURN_WINDOW_S:
                    break
                if abs(angle_diff(twd_k, twd_j)) <= _RETURN_TOL_DEG:
                    returned = True
                    break
            events.append({
                "t": t_i.isoformat(timespec="seconds"),
                "type": "oscillation" if returned else "persistent_shift",
                "magnitude_deg": round(move, 1),
                "direction": "right" if move > 0 else "left",
            })
            claimed_until = t_i + timedelta(seconds=RETURN_WINDOW_S)
            break

    # ── TWS trend over the whole series. ──────────────────────────────
    tws = [x[2] for x in parsed]
    n = len(tws)
    first = sum(tws[: max(1, n // 4)]) / max(1, n // 4)
    last = sum(tws[-max(1, n // 4):]) / max(1, n // 4)
    delta = last - first
    if abs(delta) >= TREND_MIN_KT:
        events.append({
            "t": parsed[-1][0].isoformat(timespec="seconds"),
            "type": "building" if delta > 0 else "dying",
            "magnitude_kts": round(abs(delta), 1),
        })
    return events


def detect_events(rows: list[dict]) -> list[dict]:
    """Public alias — the pre-race playbook matcher runs the same event
    detection over forecast-derived rows."""
    return _detect_events(rows)


def forecast_rows(
    forecast,
    *,
    lat: float,
    lon: float,
    t_start: datetime,
    t_end: datetime,
    interval_s: int = ROW_INTERVAL_S,
) -> list[dict]:
    """Timeline rows from a live ``WindForecast``-like (``sample(lat,
    lon, t) -> (u, v) | None``) at a fixed point — the pre-race input
    to ``summarize_conditions``. Same row shape as
    ``build_wind_timeline``, source tagged "forecast"."""
    from app.services.race_analysis.geo import uv_to_tws_twd

    rows: list[dict] = []
    t = t_start
    while t <= t_end:
        uv = forecast.sample(lat, lon, t) if forecast is not None else None
        if uv is not None:
            tws, twd = uv_to_tws_twd(uv[0], uv[1])
            rows.append({
                "t": t.isoformat(timespec="seconds"),
                "twd_deg": round(twd, 1),
                "tws_kts": round(tws, 1),
                "gust_kts": None,
                "source": "forecast",
            })
        t += timedelta(seconds=interval_s)
    return rows


def summarize_conditions(rows: list[dict], events: list[dict]) -> Optional[dict]:
    """Headline numbers for the signature: TWS band, mean TWD, character."""
    if not rows:
        return None
    tws = [r["tws_kts"] for r in rows]
    from app.services.race_analysis.geo import circular_mean_deg
    twd_mean = circular_mean_deg([r["twd_deg"] for r in rows])
    shifts = [e for e in events if e["type"] in ("persistent_shift", "oscillation")]
    osc = [e for e in shifts if e["type"] == "oscillation"]
    persistent = [e for e in shifts if e["type"] == "persistent_shift"]
    if persistent:
        rights = sum(1 for e in persistent if e.get("direction") == "right")
        character = (
            "persistent_right" if rights * 2 >= len(persistent) else "persistent_left"
        )
    elif osc:
        character = "oscillating"
    else:
        character = "steady"
    trend = next(
        (e["type"] for e in events if e["type"] in ("building", "dying")), "steady",
    )
    return {
        "tws_lo_kts": round(min(tws), 1),
        "tws_hi_kts": round(max(tws), 1),
        "twd_mean_deg": round(twd_mean, 1) if twd_mean is not None else None,
        "character": character,
        "osc_amplitude_deg": (
            round(max(abs(e["magnitude_deg"]) for e in osc), 1) if osc else None
        ),
        "tws_trend": trend,
    }


__all__ = [
    "build_wind_timeline", "summarize_conditions",
    "detect_events", "forecast_rows",
    "ROW_INTERVAL_S", "SHIFT_MIN_DEG", "TREND_MIN_KT",
]
