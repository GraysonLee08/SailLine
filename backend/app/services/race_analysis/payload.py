"""Assemble the single ``<race_data>`` JSON object the analysis prompt
consumes. Derived numbers only — never raw track arrays.

Entry point: ``build_race_analysis(...)``. Pure function over inputs
the worker loads (track rows, IMU rows, race row fields, snapshots,
polar, tactician calls). Sections that can't be computed are omitted —
the prompt is contractually forbidden from commenting on absent data.

Token budget: the wind timeline self-downsamples for long races
(wind_timeline._MAX_ROWS); everything else is O(legs + marks + calls).
Typical buoy race lands well inside the 3–6 K token target.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app.services.race_analysis.geo import FT_TO_M
from app.services.race_analysis.calls import replay_calls
from app.services.race_analysis.laylines import analyze_laylines, optimal_twa
from app.services.race_analysis.leeway import analyze_leeway
from app.services.race_analysis.legs import segment_legs
from app.services.race_analysis.maneuvers import (
    DEFAULT_LOA_M,
    detect_maneuvers,
    summarize_maneuvers,
)
from app.services.race_analysis.preprocess import build_wind_sampler, clean_track
from app.services.race_analysis.roundings import analyze_roundings
from app.services.race_analysis.shifts import analyze_shifts
from app.services.race_analysis.signature import signature_text
from app.services.race_analysis.start import analyze_start, resolve_line_bearing
from app.services.race_analysis.wind_timeline import (
    build_wind_timeline,
    summarize_conditions,
)

log = logging.getLogger(__name__)

# Reference TWS columns for the polar summary block.
_POLAR_TWS_REF = (6.0, 10.0, 14.0, 20.0)


def _polar_summary(polar) -> Optional[dict]:
    """Compact polar targets so the model can reason about "vs target"
    numbers without the full table."""
    if polar is None:
        return None
    try:
        up: dict[str, float] = {}
        down: dict[str, float] = {}
        up_twa = optimal_twa(polar, 10.0)
        down_twa = optimal_twa(polar, 10.0, downwind=True)
        for tws in _POLAR_TWS_REF:
            ut = optimal_twa(polar, tws)
            dt = optimal_twa(polar, tws, downwind=True)
            if ut is not None:
                up[f"{tws:.0f}kt"] = round(polar.boat_speed(ut, tws), 2)
            if dt is not None:
                down[f"{tws:.0f}kt"] = round(polar.boat_speed(dt, tws), 2)
        return {
            "upwind_target_twa_deg": round(up_twa, 1) if up_twa is not None else None,
            "downwind_target_twa_deg": (
                round(down_twa, 1) if down_twa is not None else None
            ),
            "upwind_target_kts_by_tws": up or None,
            "downwind_target_kts_by_tws": down or None,
        }
    except Exception as e:  # noqa: BLE001 — a bad polar must not sink the payload
        log.warning("race_analysis: polar summary failed (%s)", e)
        return None


def _boat_block(
    *,
    boat_class: Optional[str],
    loa_ft: Optional[float],
    ratings: Optional[dict],
    polar,
) -> dict:
    block: dict = {"class": boat_class}
    if loa_ft is not None:
        block["loa_ft"] = round(loa_ft, 1)
    if ratings:
        clean = {k: v for k, v in ratings.items() if v is not None}
        if clean:
            block["ratings_s_per_nm"] = clean
    ps = _polar_summary(polar)
    if ps:
        block["polar_summary"] = ps
    return block


def _sea_state(obs_snapshot: Optional[dict]) -> Optional[dict]:
    """Mean wave height/period/direction from the nearest reporting buoy."""
    if not obs_snapshot:
        return None
    for st in sorted(
        (s for s in obs_snapshot.get("stations") or [] if s.get("obs")),
        key=lambda s: s.get("distance_km") or 1e9,
    ):
        hs = [o["wvht_m"] for o in st["obs"] if isinstance(o.get("wvht_m"), (int, float))]
        if not hs:
            continue
        dpd = [o["dpd_s"] for o in st["obs"] if isinstance(o.get("dpd_s"), (int, float))]
        mwd = [o["mwd_deg"] for o in st["obs"] if isinstance(o.get("mwd_deg"), (int, float))]
        return {
            "wave_height_m": round(sum(hs) / len(hs), 2),
            "wave_period_s": round(sum(dpd) / len(dpd), 1) if dpd else None,
            "wave_dir_deg": round(sum(mwd) / len(mwd)) if mwd else None,
            "station_distance_km": st.get("distance_km"),
        }
    return None


def build_race_analysis(
    *,
    track_rows: list[dict],
    imu_rows: list[dict],
    marks: list[dict],
    mark_passes: list[dict],
    race_start_at: Optional[datetime],
    mode: Optional[str],
    boat_class: Optional[str],
    loa_ft: Optional[float],
    ratings: Optional[dict],
    polar,
    wind_snapshot: Optional[dict],
    obs_snapshot: Optional[dict],
    performance_summary: Optional[dict],
    tactician_call_rows: list[dict],
    start_line_bearing_override: Optional[float] = None,
    start_line_bearing_deg: Optional[float] = None,
    stats: Optional[dict] = None,
) -> Optional[dict]:
    """Compute every derivable metric block. None when the track is
    unusable (fewer than ~2 min of cleaned points)."""
    wind_at, wind_source = build_wind_sampler(wind_snapshot, obs_snapshot)
    points = clean_track(track_rows, wind_at=wind_at)
    if len(points) < 120:
        log.info("race_analysis: too few cleaned points (%d)", len(points))
        return None

    loa_m = loa_ft * FT_TO_M if loa_ft else DEFAULT_LOA_M

    legs = segment_legs(points, marks=marks, mark_passes=mark_passes)
    leg_bounds = [(l.n, l.start_ts, l.end_ts) for l in legs]
    maneuvers = detect_maneuvers(points, loa_m=loa_m, leg_bounds=leg_bounds)

    # Performance by_leg is keyed by "passes so far" (0 = pre-start);
    # our leg n covers pass n−1 → pass n, i.e. perf bucket n.
    perf_by_leg = {
        b["leg_index"]: b
        for b in (performance_summary or {}).get("by_leg") or []
    }

    leg_dicts: list[dict] = []
    for leg in legs:
        d = leg.to_dict()
        perf = perf_by_leg.get(leg.n)
        if perf:
            d["speed_ratio"] = perf.get("avg_speed_ratio")
            d["vmg_efficiency"] = perf.get("avg_vmg_efficiency")
        leg_maneuvers = [m for m in maneuvers if m.leg_n == leg.n]
        d["tacks"] = sum(1 for m in leg_maneuvers if m.kind == "tack")
        d["gybes"] = sum(1 for m in leg_maneuvers if m.kind == "gybe")
        shift_block = analyze_shifts(leg, maneuvers)
        if shift_block:
            d.update({
                "pct_time_lifted": shift_block["pct_time_lifted"],
                "pct_time_headed": shift_block["pct_time_headed"],
                "missed_shifts": shift_block["missed_shifts"],
            })
        layline_block = analyze_laylines(
            leg, polar=polar, marks=marks, maneuvers=maneuvers, loa_m=loa_m,
        ) if polar is not None else None
        if layline_block:
            d["laylines"] = layline_block
        leg_dicts.append(d)

    t_start = points[0].t
    t_end = points[-1].t
    timeline_rows, wind_events = build_wind_timeline(
        points,
        wind_at=wind_at,
        obs_snapshot=obs_snapshot,
        source=wind_source,
        t_start=t_start,
        t_end=t_end,
    )
    sig = summarize_conditions(timeline_rows, wind_events)

    conditions: dict = {}
    if timeline_rows:
        conditions["wind_timeline"] = timeline_rows
        conditions["wind_events"] = wind_events
    sea = _sea_state(obs_snapshot)
    if sea:
        conditions["sea_state"] = sea

    leeway_block = analyze_leeway(points, imu_rows, legs=legs)
    if leeway_block:
        conditions["leeway"] = {
            k: v for k, v in leeway_block.items() if k != "current_inference"
        }
        if "current_inference" in leeway_block:
            conditions["current_inference"] = leeway_block["current_inference"]

    line_bearing = resolve_line_bearing(
        override=start_line_bearing_override,
        resolved=start_line_bearing_deg,
        marks=marks,
        gun=race_start_at,
        wind_at=wind_at,
    )
    start_block = analyze_start(
        points,
        marks=marks,
        gun=race_start_at,
        line_bearing_deg=line_bearing,
        wind_at=wind_at,
    )

    roundings = analyze_roundings(
        points, marks=marks, mark_passes=mark_passes, loa_m=loa_m,
    )

    calls_block = replay_calls(
        tactician_call_rows, points=points, maneuvers=maneuvers,
    )

    payload: dict = {
        "boat": _boat_block(
            boat_class=boat_class, loa_ft=loa_ft, ratings=ratings, polar=polar,
        ),
        "course": {
            "mode": mode,
            "marks_count": len(marks),
            "legs_count": len(leg_dicts),
        },
        "maneuvers": summarize_maneuvers(maneuvers),
    }
    if conditions:
        payload["conditions"] = conditions
    if sig:
        payload["condition_signature"] = sig
        payload["condition_signature_text"] = signature_text(sig)
    if start_block:
        payload["start"] = start_block
    if leg_dicts:
        payload["legs"] = leg_dicts
    if roundings:
        payload["mark_roundings"] = roundings
    if calls_block:
        payload["tactician_calls"] = calls_block
    if stats:
        result: dict = {"elapsed_s": stats.get("elapsed_s")}
        if stats.get("corrected_time_s") is not None:
            result["corrected_s"] = stats["corrected_time_s"]
            result["corrected_using"] = stats.get("corrected_using")
        payload["result"] = result
        if performance_summary:
            payload["result"]["overall_speed_ratio"] = performance_summary.get(
                "avg_speed_ratio"
            )
            payload["result"]["overall_vmg_efficiency"] = performance_summary.get(
                "avg_vmg_efficiency"
            )
            payload["result"]["pct_time_on_target"] = performance_summary.get(
                "pct_time_on_target"
            )
    return payload


__all__ = ["build_race_analysis"]
