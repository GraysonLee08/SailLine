"""Tactician orchestration — context load → detect → advise → publish.

Entry point: ``evaluate_tactics_safe(race_id, uid)`` — fire-and-forget
from the telemetry ingest path via ``asyncio.create_task``. Every
failure mode is swallowed and logged; a tactician bug can never break
telemetry ingestion.

Flow (spec 2026-06-11):

  1. Cheap gates: global cooldown (Redis SETNX), race is live
     (started, not ended), user setting not opted out.
  2. Load context: race row, last ~5 min of track, recent IMU +
     calibrations, active route (``route:current``), forecast, polar,
     last 3 calls.
  3. Build per-fix performance evals (``performance.evaluate_point``),
     heel statistic, run detectors (priority-sorted).
  4. Per-type cooldown on the winning candidate.
  5. Snapshot → Claude (in a thread — sync SDK). SILENT ⇒ stop.
  6. Staleness guard: maneuver calls re-check eta AFTER the model
     round-trip; if the event is now inside the minimum lead, the call
     is DROPPED and logged (never delivered late).
  7. Persist to ``tactician_calls``, store latest at
     ``tactics:latest:{race_id}`` (SSE replay), publish to the race's
     notification channel with ``type: "tactics"``, set cooldowns.

Latency budget: telemetry flush cadence (~30 s today) dominates; the
detector pass is ~ms and Haiku ~1–2 s. Maneuver detectors announce
2–5 min ahead, so transport latency is noise — see the spec's
"lead time is the product" section.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

log = logging.getLogger(__name__)

# ─── Tunables ────────────────────────────────────────────────────────────

# Quiet cockpit: at most one call per race per this window.
GLOBAL_COOLDOWN_S = 180
# Same call type doesn't repeat inside this window (the "one call +
# one reminder per episode" rule comes out of this plus the global gap).
PER_TYPE_COOLDOWN_S = 600

# A delivered maneuver call must still have at least this much lead.
MIN_LEAD_S = 90.0

# How much history detectors get.
TRACK_WINDOW_S = 300
IMU_WINDOW_S = 120

# SSE replay key TTL.
LATEST_CALL_TTL_S = 6 * 3600

# v1c gate — flip after the on-water heel validation race
# (spec: heel calls wait on the sanity check, not on plumbing).
HEEL_CALLS_ENABLED = False

# Forecast window for live sampling: now → +1 h covers every detector
# horizon (max look-ahead is 15 min) with margin.
FORECAST_DURATION_HOURS = 1.0


# ─── Entry point ─────────────────────────────────────────────────────────


async def evaluate_tactics_safe(race_id: UUID, uid: str) -> None:
    """Run one tactics evaluation; never raise."""
    try:
        await _evaluate(race_id, uid)
    except Exception:  # noqa: BLE001
        log.exception("tactician: evaluation failed race=%s", race_id)


async def _evaluate(race_id: UUID, uid: str) -> None:
    # Imports are local so a failure in any heavy dependency chain
    # degrades to "tactician off" instead of breaking app startup —
    # same defensive posture as the weather→routing lazy import.
    from app import db, redis_client
    from app.services.redis_keys import (
        route_current_key,
        tactics_cooldown_key,
        tactics_latest_key,
        route_notifications_channel,
    )

    redis = redis_client.get_client()
    now = datetime.now(timezone.utc)

    # 1 ── global cooldown gate (SETNX: first writer wins the window).
    acquired = await redis.set(
        tactics_cooldown_key(race_id), b"1",
        ex=GLOBAL_COOLDOWN_S, nx=True,
    )
    if not acquired:
        return

    # db.get_pool() raises when the pool is unavailable (non-fatal
    # startup pattern) — the safe wrapper turns that into a logged skip.
    pool = db.get_pool()

    # 2 ── context loads.
    async with pool.acquire() as conn:
        race = await conn.fetchrow(
            """
            SELECT r.name, r.marks, r.mark_passes, r.start_at, r.started_at,
                   r.ended_at, r.mode, r.boat_class
            FROM race_sessions r
            WHERE r.id = $1
            """,
            race_id,
        )
        if race is None or race["ended_at"] is not None:
            return
        # Live only: don't coach a boat that hasn't started recording.
        if race["started_at"] is None and (
            race["start_at"] is None or race["start_at"] > now
        ):
            return

        # Per-user opt-out (settings sync to user_profiles.app_settings).
        settings_row = await conn.fetchrow(
            "SELECT app_settings FROM user_profiles WHERE id = $1",
            uid,
        )
        if settings_row is not None:
            app_settings = settings_row["app_settings"] or {}
            if isinstance(app_settings, str):
                try:
                    app_settings = json.loads(app_settings)
                except ValueError:
                    app_settings = {}
            tact = app_settings.get("tactician") or {}
            if isinstance(tact, dict) and tact.get("enabled") is False:
                return

        track_rows = await conn.fetch(
            """
            SELECT recorded_at,
                   ST_Y(position::geometry) AS lat,
                   ST_X(position::geometry) AS lon,
                   speed_kts, heading_deg
            FROM track_points
            WHERE session_id = $1 AND recorded_at >= $2
            ORDER BY recorded_at
            """,
            race_id, now - timedelta(seconds=TRACK_WINDOW_S),
        )
        imu_rows = await conn.fetch(
            """
            SELECT recorded_at, heel_deg
            FROM imu_samples
            WHERE session_id = $1 AND recorded_at >= $2
            ORDER BY recorded_at
            """,
            race_id, now - timedelta(seconds=IMU_WINDOW_S),
        )
        cal_rows = await conn.fetch(
            """
            SELECT captured_at, heel_zero_offset_deg, pitch_zero_offset_deg
            FROM race_calibrations
            WHERE session_id = $1
            ORDER BY captured_at
            """,
            race_id,
        )
        recent_call_rows = await conn.fetch(
            """
            SELECT created_at, call_type, message
            FROM tactician_calls
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT 3
            """,
            race_id,
        )

    track = [
        {"t": r["recorded_at"], "lat": r["lat"], "lon": r["lon"],
         "sog_kts": r["speed_kts"], "cog_deg": r["heading_deg"]}
        for r in track_rows
    ]
    if len(track) < 3:
        return  # not enough live data to say anything responsible

    marks = _coerce_jsonb_list(race["marks"])
    passes = _coerce_jsonb_list(race["mark_passes"])
    next_mark = None
    if marks and len(passes) < len(marks):
        m = marks[len(passes)]
        next_mark = {"lat": float(m["lat"]), "lon": float(m["lon"]),
                     "label": m.get("name")}

    # 3 ── forecast + polar + evals + heel + route.
    forecast = await _load_forecast(track, now)
    if forecast is None:
        return  # no wind context ⇒ no trustworthy calls

    from app.services.polars import load_polar_for_class
    polar = load_polar_for_class(race["boat_class"])

    from app.services.performance import evaluate_point
    evals: list[dict] = []
    for p in track:
        uv = forecast.sample(p["lat"], p["lon"], p["t"])
        ev = evaluate_point(
            polar, sog_kts=p["sog_kts"], cog_deg=p["cog_deg"], wind_uv=uv,
        )
        if ev is not None:
            ev["t"] = p["t"]
            evals.append(ev)

    from app.services.tactics.heel import sustained_heel
    heel_stat = sustained_heel(
        [dict(r) for r in imu_rows],
        calibrations=[dict(r) for r in cal_rows],
        now=now,
    )

    route_coords = None
    route_blob = await redis.get(route_current_key(race_id))
    if route_blob:
        try:
            feature = json.loads(
                route_blob.decode() if isinstance(route_blob, bytes)
                else route_blob
            )
            geom = (feature or {}).get("geometry") or {}
            if geom.get("type") == "LineString":
                route_coords = [tuple(c[:2]) for c in geom["coordinates"]]
        except (ValueError, TypeError):
            route_coords = None

    # 4 ── detect + pick.
    from app.services.tactics.detectors import run_detectors
    candidates = run_detectors(
        track=track, evals=evals, forecast=forecast, polar=polar,
        route_coords=route_coords, next_mark=next_mark,
        heel_stat=heel_stat, boat_class=race["boat_class"], now=now,
        include_heel=HEEL_CALLS_ENABLED,
    )
    winner = None
    for cand in candidates:
        type_ok = await redis.set(
            tactics_cooldown_key(race_id, cand.call_type), b"1",
            ex=PER_TYPE_COOLDOWN_S, nx=True,
        )
        if type_ok:
            winner = cand
            break
    if winner is None:
        return

    # 5 ── snapshot → Claude (thread: the SDK client is sync).
    from app.services.tactics.snapshot import build_snapshot
    from app.services.tactics import advisor

    snapshot = build_snapshot(
        candidate=winner,
        other_candidates=[c for c in candidates if c is not winner],
        race_meta={
            "race_name": race["name"],
            "boat_class": race["boat_class"],
            "mode": race["mode"],
            "leg_index": len(passes),
            "marks_total": len(marks),
        },
        track=track, evals=evals, forecast=forecast,
        next_mark=next_mark, heel_stat=heel_stat,
        recent_calls=[
            {"created_at": r["created_at"].isoformat(timespec="seconds"),
             "call_type": r["call_type"], "message": r["message"]}
            for r in recent_call_rows
        ],
        now=now,
    )
    call = await asyncio.to_thread(advisor.generate_call, snapshot)
    if call is None:
        return  # SILENT or advisor failure — quiet cockpit wins

    # 6 ── staleness guard, enforced in code AFTER the model round-trip.
    post_now = datetime.now(timezone.utc)
    if winner.eta is not None:
        remaining = (winner.eta - post_now).total_seconds()
        if remaining < MIN_LEAD_S:
            log.info(
                "tactician: DROPPED late %s call race=%s (%.0fs lead < %.0fs)",
                winner.call_type, race_id, remaining, MIN_LEAD_S,
            )
            return

    # 7 ── persist + publish.
    payload = {
        "type": "tactics",
        "race_id": str(race_id),
        "call_type": winner.call_type,
        "call_class": winner.call_class,
        "message": call["message"],
        "eta": winner.eta.isoformat() if winner.eta else None,
        "diagnosis": winner.diagnosis,
        "model": call["model"],
        "prompt_version": call["prompt_version"],
        "created_at": post_now.isoformat(),
    }
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tactician_calls
                (session_id, call_type, call_class, eta, message,
                 diagnosis, model, prompt_version)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            race_id, winner.call_type, winner.call_class, winner.eta,
            call["message"], winner.diagnosis,  # dict — global JSONB codec
            call["model"], call["prompt_version"],
        )
    blob = json.dumps(payload).encode()
    await redis.setex(tactics_latest_key(race_id), LATEST_CALL_TTL_S, blob)
    await redis.publish(route_notifications_channel(race_id), blob)
    log.info(
        "tactician: %s call published race=%s msg=%r",
        winner.call_type, race_id, call["message"],
    )


# ─── Small helpers ───────────────────────────────────────────────────────


def _coerce_jsonb_list(value) -> list:
    """Tolerate legacy double-encoded JSONB rows (see 2026-06-09
    session): str → parse; non-list → []."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, str):  # double-encoded
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


async def _load_forecast(track: list[dict], now: datetime):
    """Live forecast at the boat — region from the latest fix.
    Returns None (logged) on any unavailability."""
    from app.regions import base_region_for_point
    from app.services.weather.forecast_loader import (
        ForecastNotAvailable,
        load_forecast_for_race,
    )
    last = track[-1]
    region = base_region_for_point(last["lat"], last["lon"])
    if region is None:
        return None
    try:
        return await load_forecast_for_race(
            region.name, now, duration_hours=FORECAST_DURATION_HOURS,
        )
    except ForecastNotAvailable as e:
        log.info("tactician: forecast unavailable (%s)", e)
        return None
    except Exception:  # noqa: BLE001
        log.exception("tactician: forecast load failed")
        return None
