"""Tactical-snapshot builder — the JSON document the advisor reasons over.

Pure function. The pipeline assembles the inputs (it owns all I/O);
this module just shapes them. Budget ~1–2k tokens: the track is
decimated, evals are summarised, and only the numbers the prompt
contract allows Claude to use are included.

Spec: detectors decide *when*, Claude reasons over THIS document to
decide *what to say* (or SILENT). The snapshot therefore carries more
than the winning candidate — recent performance, wind now/ahead, and
the last few calls (so the advisor doesn't repeat itself) — but every
number is server-computed. Claude never sees raw sensor streams.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from app.services.tactics.detectors import CallCandidate

# Decimation step for the recent-track block. 15 s steps over 5 min
# = ≤20 rows — enough to show the shape of the last beat without
# blowing the token budget.
_TRACK_STEP_S = 15.0
_TRACK_SPAN_S = 300.0


def _decimate_track(track: list[dict], now: datetime) -> list[dict]:
    cutoff = now - timedelta(seconds=_TRACK_SPAN_S)
    out: list[dict] = []
    last_kept: Optional[datetime] = None
    for p in track:
        t = p.get("t")
        if t is None or t < cutoff:
            continue
        if last_kept is not None and (t - last_kept).total_seconds() < _TRACK_STEP_S:
            continue
        last_kept = t
        out.append({
            "t": t.isoformat(timespec="seconds"),
            "sog_kts": p.get("sog_kts"),
            "cog_deg": p.get("cog_deg"),
        })
    return out


def _summarise_evals(evals: list[dict], now: datetime) -> Optional[dict]:
    cutoff = now - timedelta(seconds=120)
    win = [e for e in evals if e.get("t") is not None and e["t"] >= cutoff]
    if not win:
        return None
    ratios = [e["speed_ratio"] for e in win if e.get("speed_ratio") is not None]
    latest = win[-1]
    return {
        "latest": {
            k: latest.get(k)
            for k in ("twa", "tws_kts", "twd", "target_kts", "actual_kts",
                      "speed_ratio", "vmg_ratio")
        },
        "mean_speed_ratio_2min":
            round(sum(ratios) / len(ratios), 3) if ratios else None,
        "samples_2min": len(win),
    }


def _wind_ahead(
    forecast, lat: float, lon: float, now: datetime,
) -> list[dict]:
    """Forecast wind at the boat's position now / +5 / +10 / +15 min."""
    from app.services.tactics.detectors import _uv_to_tws_twd
    out: list[dict] = []
    for mins in (0, 5, 10, 15):
        t = now + timedelta(minutes=mins)
        uv = forecast.sample(lat, lon, t) if forecast else None
        if uv is None:
            continue
        tws, twd = _uv_to_tws_twd(uv[0], uv[1])
        out.append({"in_min": mins, "tws_kt": round(tws, 1),
                    "twd_deg": round(twd)})
    return out


def build_snapshot(
    *,
    candidate: CallCandidate,
    other_candidates: Sequence[CallCandidate],
    race_meta: dict,
    track: list[dict],
    evals: list[dict],
    forecast,
    next_mark: Optional[dict],
    heel_stat: Optional[dict],
    recent_calls: list[dict],
    now: datetime,
    playbook: Optional[dict] = None,
) -> dict:
    """Assemble the advisor's input document.

    ``race_meta`` is pipeline-shaped: ``{race_name, boat_class, mode,
    leg_index, marks_total}``. ``recent_calls`` is the last ≤3 rows
    from ``tactician_calls`` as ``{created_at, call_type, message}``.
    ``playbook`` (optional) is the matched pre-race brief from
    ``tactics.playbook``: ``{signature_text, source_race_name, score,
    directives}`` — this boat's own directives from a past race in
    similar conditions.
    """
    last = track[-1] if track else None

    def _cand(c: CallCandidate) -> dict:
        d: dict = {
            "call_type": c.call_type,
            "call_class": c.call_class,
            "diagnosis": c.diagnosis,
        }
        if c.eta is not None:
            d["eta"] = c.eta.isoformat(timespec="seconds")
            d["seconds_until_event"] = round((c.eta - now).total_seconds())
        if c.adjustments:
            d["candidate_adjustments"] = list(c.adjustments)
        return d

    snapshot: dict = {
        "now": now.isoformat(timespec="seconds"),
        "race": race_meta,
        "trigger": _cand(candidate),
        "also_detected": [_cand(c) for c in other_candidates],
        "recent_track": _decimate_track(track, now),
        "performance": _summarise_evals(evals, now),
        "wind_at_boat": (
            _wind_ahead(forecast, last["lat"], last["lon"], now)
            if last else []
        ),
        "next_mark": (
            {"label": next_mark.get("label"),
             "distance_m": None}  # filled by pipeline when computable
            if next_mark else None
        ),
        "recent_calls": recent_calls,
    }
    if heel_stat is not None:
        snapshot["heel"] = heel_stat
    if playbook is not None and playbook.get("directives"):
        snapshot["playbook"] = {
            "conditions": playbook.get("signature_text"),
            "from_race": playbook.get("source_race_name"),
            "directives": playbook["directives"],
        }
    # Next-mark distance, when we have both a fix and a mark.
    if last and next_mark:
        from app.services.tactics.detectors import _haversine_m, _bearing_deg
        snapshot["next_mark"]["distance_m"] = round(
            _haversine_m(last["lat"], last["lon"],
                         next_mark["lat"], next_mark["lon"]))
        snapshot["next_mark"]["bearing_deg"] = round(
            _bearing_deg(last["lat"], last["lon"],
                         next_mark["lat"], next_mark["lon"]))
    return snapshot
