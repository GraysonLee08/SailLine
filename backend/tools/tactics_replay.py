"""Tactician replay harness (Phase B, 2026-07-09).

Replays a recorded race through the tactician's decision sequence —
windowed track → evals → heel → detectors → cooldowns → snapshot →
advisor → staleness — batch-by-batch with a simulated clock, entirely
offline (no Redis, no Cloud SQL, no network unless ``--real-claude``).

Why: driving/walking sims can never exercise the detectors (polar and
geometry assume a sailboat), and race nights are a slow feedback loop.
This replays real recorded races at the desk so thresholds are tuned
against evidence, not guesses.

Usage (from ``backend/``, venv active)::

    # Full export (race row + track + imu + wind_snapshot), see the
    # Cloud SQL Studio query in sailline -docs/2026-07-09_session.md:
    python -m tools.tactics_replay --export beer_can_7_1.json

    # Committed test fixture + synthetic steady wind (fixtures carry
    # no wind snapshot; "270@12" = wind FROM 270° true at 12 kt):
    python -m tools.tactics_replay ^
        --fixture tests/fixtures/beer_can_race_4_20260603.json ^
        --wind 270@12

    # Options: --batch-seconds 30, --include-heel (preview v1c),
    # --route feature.json (active-route GeoJSON for the plan-based
    # detectors), --real-claude (needs ANTHROPIC_API_KEY in .env),
    # --json out.json (machine-readable, diffable across threshold
    # experiments), --heel-report.

Fidelity notes (accepted, flagged as debt in the session log):

* The orchestration below intentionally mirrors ``pipeline
  ._evaluate_inner``'s decision sequence (~30 lines) instead of
  refactoring the live pipeline for injectable clock/IO. Constants are
  imported from ``pipeline`` so a tuning change shows up here
  automatically; the sequence itself can drift — compare on change.
* Cooldowns are simulated with plain timestamps, matching the live
  SETNX semantics: global acquired per evaluation and released on
  transient exits; per-type acquired by the winning candidate only.
* No simulated advisor latency: the staleness guard is checked against
  the same sim-clock instant. Live drops caused by slow Claude calls
  won't reproduce here.
* Mark progression replays recorded ``mark_passes`` timestamps. A
  race with empty passes (e.g. the 07-01 detector failure) keeps
  ``next_mark`` = first unpassed mark for the whole replay.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from app.services.performance import evaluate_point
from app.services.polars import load_polar_for_class
from app.services.tactics import advisor
from app.services.tactics.detectors import run_detectors
from app.services.tactics.heel import sustained_heel
from app.services.tactics.heel_bands import band_for
from app.services.tactics.pipeline import (
    GLOBAL_COOLDOWN_S,
    IMU_WINDOW_S,
    MIN_LEAD_S,
    PER_TYPE_COOLDOWN_S,
    TRACK_WINDOW_S,
)
from app.services.tactics.snapshot import build_snapshot
from app.services.wind_snapshot import snapshot_sampler

_KT_TO_MS = 0.514444


# ─── Wind sources ────────────────────────────────────────────────────────


class SnapshotWind:
    """Forecast-like adapter over a persisted ``race_sessions
    .wind_snapshot`` — the same frozen grid post-race analysis scores
    against, exposed through the ``.sample(lat, lon, t)`` protocol the
    detectors and ``evaluate_point`` consume."""

    quality = "snapshot"

    def __init__(self, snapshot: dict) -> None:
        self._sample = snapshot_sampler(snapshot)

    def sample(self, lat: float, lon: float, t: datetime):
        return self._sample(lat, lon, t)


class SteadyWind:
    """Uniform wind everywhere/always — for fixtures with no snapshot.
    Forecast-shift and divergence detectors are inert under it (by
    construction there is nothing to shift), which the summary notes."""

    quality = "synthetic"

    def __init__(self, twd_deg: float, tws_kt: float) -> None:
        self.twd_deg = twd_deg
        self.tws_kt = tws_kt
        to_rad = math.radians((twd_deg + 180.0) % 360.0)
        ms = tws_kt * _KT_TO_MS
        self._uv = (ms * math.sin(to_rad), ms * math.cos(to_rad))

    def sample(self, lat: float, lon: float, t: datetime):
        return self._uv


# ─── Input loading ───────────────────────────────────────────────────────


def _parse_iso(value: str) -> datetime:
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Python < 3.11 requires exactly 3 or 6 fractional digits; the
        # mobile recorder emits whatever the fix had (e.g. ``.88``).
        s = re.sub(
            r"\.(\d{1,6})\d*",
            lambda m: "." + m.group(1).ljust(6, "0"),
            s, count=1,
        )
        dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _derive_cogs(points: list[dict]) -> None:
    """Fill missing ``cog_deg`` from the bearing to the next fix (the
    committed fixtures carry no heading). Last point copies its
    predecessor. In-place."""
    for i, p in enumerate(points):
        if p.get("cog_deg") is not None:
            continue
        if i + 1 < len(points):
            q = points[i + 1]
            p["cog_deg"] = _bearing_deg(p["lat"], p["lon"], q["lat"], q["lon"])
        elif i > 0:
            p["cog_deg"] = points[i - 1]["cog_deg"]
        else:
            p["cog_deg"] = 0.0


def load_export(path: Path) -> dict:
    """Load a Cloud SQL Studio export: ``{race, track, imu,
    calibrations}`` (see the session log for the query)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Studio wraps the single row as [{"export": {...}}] sometimes.
    if isinstance(raw, list):
        raw = raw[0]
    if "export" in raw:
        raw = raw["export"]
    if isinstance(raw, str):
        raw = json.loads(raw)

    race = raw["race"] or {}
    track = [
        {"t": _parse_iso(p["t"]), "lat": float(p["lat"]),
         "lon": float(p["lon"]),
         "sog_kts": p.get("sog_kts"), "cog_deg": p.get("cog_deg")}
        for p in (raw.get("track") or [])
    ]
    _derive_cogs(track)
    imu = [
        {"recorded_at": _parse_iso(s["t"]), "heel_deg": s.get("heel_deg")}
        for s in (raw.get("imu") or [])
    ]
    cals = [
        {"captured_at": _parse_iso(c["captured_at"]),
         "heel_zero_offset_deg": c.get("heel_zero_offset_deg"),
         "pitch_zero_offset_deg": c.get("pitch_zero_offset_deg")}
        for c in (raw.get("calibrations") or [])
    ]
    return {
        "name": race.get("name") or path.stem,
        "boat_class": race.get("boat_class"),
        "mode": race.get("mode"),
        "marks": race.get("marks") or [],
        "mark_passes": race.get("mark_passes") or [],
        "wind_snapshot": race.get("wind_snapshot"),
        "track": track,
        "imu": imu,
        "calibrations": cals,
    }


def load_fixture(path: Path) -> dict:
    """Load a committed test fixture (``beer_can_race_4_20260603.json``
    shape: race meta + ``points`` with ts/lat/lon/sog_kts)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    track = [
        {"t": _parse_iso(p["ts"]), "lat": float(p["lat"]),
         "lon": float(p["lon"]),
         "sog_kts": p.get("sog_kts"), "cog_deg": p.get("cog_deg")}
        for p in raw["points"]
    ]
    _derive_cogs(track)
    return {
        "name": raw.get("course") or path.stem,
        "boat_class": raw.get("boat_class"),
        "mode": raw.get("mode"),
        "marks": raw.get("marks") or [],
        "mark_passes": raw.get("mark_passes") or [],
        "wind_snapshot": None,
        "track": track,
        "imu": [],
        "calibrations": [],
    }


def load_route(path: Path) -> Optional[list[tuple[float, float]]]:
    """GeoJSON Feature (the shape ``route:current`` stores) →
    coordinate list for the plan-based detectors."""
    feature = json.loads(path.read_text(encoding="utf-8"))
    geom = (feature or {}).get("geometry") or {}
    if geom.get("type") != "LineString":
        return None
    return [tuple(c[:2]) for c in geom["coordinates"]]


# ─── Advisors ────────────────────────────────────────────────────────────


def stub_advisor(snapshot: dict, winner) -> Optional[dict]:
    """Deterministic, zero-cost stand-in for Claude: formats the
    winning candidate's diagnosis. Never SILENT — replay wants to see
    every call the detectors would surface."""
    diag = ", ".join(
        f"{k}={v}" for k, v in sorted(winner.diagnosis.items())
    )[:100]
    return {
        "message": f"[stub {winner.call_type}] {diag}",
        "model": "stub",
        "prompt_version": 0,
    }


def real_advisor(snapshot: dict, winner) -> Optional[dict]:
    return advisor.generate_call(snapshot)


# ─── Replay core ─────────────────────────────────────────────────────────


def replay(
    data: dict,
    *,
    wind,
    batch_seconds: int = 30,
    include_heel: bool = False,
    route_coords: Optional[list[tuple[float, float]]] = None,
    advisor_fn: Callable[[dict, object], Optional[dict]] = stub_advisor,
) -> dict:
    """Run the decision sequence over the whole track. Returns a report
    ``{meta, evaluations, summary}`` — pure function of its inputs when
    ``advisor_fn`` is the stub."""
    track_all = data["track"]
    if len(track_all) < 3:
        raise ValueError("track has fewer than 3 points — nothing to replay")

    polar = load_polar_for_class(data.get("boat_class") or "")
    marks = data.get("marks") or []
    passes = sorted(
        (p for p in (data.get("mark_passes") or []) if p.get("ts")),
        key=lambda p: p["ts"],
    )
    pass_times = [_parse_iso(p["ts"]) for p in passes]

    t0 = track_all[0]["t"]
    t_end = track_all[-1]["t"]
    step = timedelta(seconds=batch_seconds)

    global_until: Optional[datetime] = None
    per_type_until: dict[str, datetime] = {}
    recent_calls: list[dict] = []
    evaluations: list[dict] = []

    ti = 0  # advancing left cursor into track_all (sorted by time)
    sim_now = t0 + step
    while sim_now <= t_end:
        rec: dict = {"t": sim_now.isoformat(timespec="seconds"),
                     "exit": None, "gates": {}, "candidates": [],
                     "call": None}
        evaluations.append(rec)

        # 1 ── global cooldown (mirrors SETNX-first semantics).
        if global_until is not None and sim_now < global_until:
            rec["exit"] = "cooldown_global"
            sim_now += step
            continue
        global_until = sim_now + timedelta(seconds=GLOBAL_COOLDOWN_S)

        # 2 ── windowed context.
        lo = sim_now - timedelta(seconds=TRACK_WINDOW_S)
        while ti < len(track_all) and track_all[ti]["t"] < lo:
            ti += 1
        window = [p for p in track_all[ti:] if p["t"] <= sim_now]
        imu_lo = sim_now - timedelta(seconds=IMU_WINDOW_S)
        imu_window = [
            s for s in data["imu"] if imu_lo <= s["recorded_at"] <= sim_now
        ]
        rec["gates"]["track_points"] = len(window)
        rec["gates"]["imu_samples"] = len(imu_window)
        if len(window) < 3:
            global_until = sim_now  # transient exit releases (pipeline parity)
            rec["exit"] = "insufficient_track"
            sim_now += step
            continue

        # 3 ── evals + heel + next mark.
        evals: list[dict] = []
        for p in window:
            uv = wind.sample(p["lat"], p["lon"], p["t"])
            ev = evaluate_point(
                polar, sog_kts=p["sog_kts"], cog_deg=p["cog_deg"], wind_uv=uv,
            )
            if ev is not None:
                ev["t"] = p["t"]
                evals.append(ev)
        heel_stat = sustained_heel(
            imu_window, calibrations=data["calibrations"], now=sim_now,
        )
        n_passed = sum(1 for pt in pass_times if pt <= sim_now)
        next_mark = None
        if marks and n_passed < len(marks):
            m = marks[n_passed]
            next_mark = {"lat": float(m["lat"]), "lon": float(m["lon"]),
                         "label": m.get("name")}

        # 4 ── detect + per-type cooldown pick.
        candidates = run_detectors(
            track=window, evals=evals, forecast=wind, polar=polar,
            route_coords=route_coords, next_mark=next_mark,
            heel_stat=heel_stat, boat_class=data.get("boat_class"),
            now=sim_now, include_heel=include_heel,
        )
        winner = None
        for cand in candidates:
            until = per_type_until.get(cand.call_type)
            if until is None or sim_now >= until:
                winner = cand
                per_type_until[cand.call_type] = (
                    sim_now + timedelta(seconds=PER_TYPE_COOLDOWN_S)
                )
                break
        rec["candidates"] = [
            {"type": c.call_type, "class": c.call_class,
             "eta": c.eta.isoformat(timespec="seconds") if c.eta else None,
             "diagnosis": c.diagnosis, "won": c is winner}
            for c in candidates
        ]
        if winner is None:
            rec["exit"] = "cooldown_type" if candidates else "no_candidates"
            sim_now += step
            continue

        # 5 ── snapshot → advisor.
        snapshot = build_snapshot(
            candidate=winner,
            other_candidates=[c for c in candidates if c is not winner],
            race_meta={
                "race_name": data.get("name"),
                "boat_class": data.get("boat_class"),
                "mode": data.get("mode"),
                "leg_index": n_passed,
                "marks_total": len(marks),
            },
            track=window, evals=evals, forecast=wind,
            next_mark=next_mark, heel_stat=heel_stat,
            playbook=None, recent_calls=recent_calls[-3:], now=sim_now,
        )
        call = advisor_fn(snapshot, winner)
        if call is None:
            rec["exit"] = "advisor_silent_or_failed"
            sim_now += step
            continue

        # 6 ── staleness (sim clock; no advisor latency simulated).
        if winner.eta is not None:
            if (winner.eta - sim_now).total_seconds() < MIN_LEAD_S:
                rec["exit"] = "dropped_late"
                sim_now += step
                continue

        # 7 ── "publish".
        rec["exit"] = "published"
        rec["call"] = {"call_type": winner.call_type,
                       "call_class": winner.call_class,
                       "message": call["message"], "model": call["model"]}
        recent_calls.append({
            "created_at": sim_now.isoformat(timespec="seconds"),
            "call_type": winner.call_type, "message": call["message"],
        })
        sim_now += step

    return {
        "meta": {
            "race": data.get("name"),
            "boat_class": data.get("boat_class"),
            "wind": wind.quality,
            "batch_seconds": batch_seconds,
            "include_heel": include_heel,
            "route": route_coords is not None,
            "track_points": len(track_all),
            "window": [t0.isoformat(timespec="seconds"),
                       t_end.isoformat(timespec="seconds")],
        },
        "evaluations": evaluations,
        "summary": summarize(evaluations),
    }


def summarize(evaluations: list[dict]) -> dict:
    exits = Counter(e["exit"] for e in evaluations)
    fired: Counter = Counter()
    won: Counter = Counter()
    blocked: Counter = Counter()
    for e in evaluations:
        for c in e["candidates"]:
            fired[c["type"]] += 1
            if c["won"]:
                won[c["type"]] += 1
        if e["exit"] == "cooldown_type":
            for c in e["candidates"]:
                blocked[c["type"]] += 1
    calls = [
        {"t": e["t"], **e["call"]} for e in evaluations if e["call"]
    ]
    return {
        "evaluations": len(evaluations),
        "exits": dict(exits),
        "detectors": {
            t: {"fired": fired[t], "won": won[t],
                "cooldown_blocked": blocked[t]}
            for t in sorted(fired)
        },
        "calls": calls,
    }


def heel_report(data: dict, report: dict) -> dict:
    """Desk half of the v1c flag-flip evidence: how often sustained
    heel sat above/in/below the boat's band, and mount quality."""
    out = {"windows": 0, "mount_ok": 0, "over": 0, "in_band": 0,
           "under": 0, "no_band": 0, "medians": []}
    boat_class = data.get("boat_class")
    # Walks the IMU stream in rolling windows independent of the main
    # replay (heel_stat isn't kept per evaluation record).
    if not data["imu"]:
        return out
    t0 = data["imu"][0]["recorded_at"]
    t_end = data["imu"][-1]["recorded_at"]
    sim = t0 + timedelta(seconds=IMU_WINDOW_S)
    while sim <= t_end:
        lo = sim - timedelta(seconds=IMU_WINDOW_S)
        win = [s for s in data["imu"] if lo <= s["recorded_at"] <= sim]
        stat = sustained_heel(
            win, calibrations=data["calibrations"], now=sim,
        )
        if stat:
            out["windows"] += 1
            if stat.get("mount_ok"):
                out["mount_ok"] += 1
            med = stat.get("median_abs_deg")
            out["medians"].append(round(med, 1) if med is not None else None)
            # Upwind band at a nominal 12 kt — the report is a coarse
            # distribution check, not the live detector (which uses
            # actual TWS/TWA per window).
            band = band_for(boat_class, tws_kt=12.0, twa_deg=45.0)
            if band is None or med is None:
                out["no_band"] += 1
            elif med > band.band_hi_deg:
                out["over"] += 1
            elif med < band.band_lo_deg:
                out["under"] += 1
            else:
                out["in_band"] += 1
        sim += timedelta(seconds=IMU_WINDOW_S)
    return out


# ─── CLI ─────────────────────────────────────────────────────────────────


def _print_report(report: dict, *, verbose: bool) -> None:
    meta = report["meta"]
    print(f"race: {meta['race']}  boat: {meta['boat_class']}  "
          f"wind: {meta['wind']}  batch: {meta['batch_seconds']}s  "
          f"route: {meta['route']}")
    if verbose:
        for e in report["evaluations"]:
            cands = " ".join(
                ("*" if c["won"] else "") + c["type"]
                for c in e["candidates"]
            )
            line = f"{e['t'][11:19]}  {e['exit']:<24} {cands}"
            if e["call"]:
                line += f"  → {e['call']['message']}"
            print(line)
    s = report["summary"]
    print(f"\nevaluations: {s['evaluations']}")
    print("exits:")
    for k, v in sorted(s["exits"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:<26}{v}")
    print("detectors:")
    for t, d in s["detectors"].items():
        print(f"  {t:<20}fired={d['fired']:<5}won={d['won']:<5}"
              f"cooldown_blocked={d['cooldown_blocked']}")
    print(f"calls ({len(s['calls'])}):")
    for c in s["calls"]:
        print(f"  {c['t'][11:19]}  [{c['call_type']}] {c['message']}")
    if report["meta"]["wind"] == "synthetic":
        print("\nnote: synthetic wind — forecast_shift/divergence "
              "detectors are inert under uniform wind.")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tactics_replay", description=__doc__.split("\n\n")[0],
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--export", type=Path, help="Cloud SQL Studio export JSON")
    src.add_argument("--fixture", type=Path, help="committed test fixture JSON")
    ap.add_argument("--wind", help='synthetic wind "TWD@KTS", e.g. 270@12 '
                    "(required when the input has no wind_snapshot)")
    ap.add_argument("--route", type=Path, help="active-route GeoJSON Feature")
    ap.add_argument("--batch-seconds", type=int, default=30)
    ap.add_argument("--include-heel", action="store_true",
                    help="preview v1c heel calls (ignores the prod flag)")
    ap.add_argument("--real-claude", action="store_true",
                    help="call the real advisor (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--json", type=Path, help="write full report JSON here")
    ap.add_argument("--heel-report", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="summary only, no per-evaluation lines")
    args = ap.parse_args(argv)

    data = load_export(args.export) if args.export else load_fixture(args.fixture)

    if data.get("wind_snapshot"):
        wind = SnapshotWind(data["wind_snapshot"])
    elif args.wind:
        twd, _, kts = args.wind.partition("@")
        wind = SteadyWind(float(twd), float(kts or 12))
    else:
        print("input has no wind_snapshot — pass --wind TWD@KTS",
              file=sys.stderr)
        return 2

    route_coords = load_route(args.route) if args.route else None

    report = replay(
        data,
        wind=wind,
        batch_seconds=args.batch_seconds,
        include_heel=args.include_heel,
        route_coords=route_coords,
        advisor_fn=real_advisor if args.real_claude else stub_advisor,
    )
    _print_report(report, verbose=not args.quiet)

    if args.heel_report:
        hr = heel_report(data, report)
        print(f"\nheel: windows={hr['windows']} mount_ok={hr['mount_ok']} "
              f"over={hr['over']} in_band={hr['in_band']} "
              f"under={hr['under']} no_band={hr['no_band']}")

    if args.json:
        args.json.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8",
        )
        print(f"\nreport written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
