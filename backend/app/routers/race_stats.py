"""Post-race stats endpoint.

``GET  /api/races/{race_id}/stats`` — return the computed stats for
a race plus the persisted AI summary and wind snapshot. The stats
themselves are recomputed from raw track points on every call (cheap:
~7k haversine ops on a 2 h race) and cached in Redis for 1 h keyed
on the raw point count. The AI summary and wind snapshot live on the
``race_sessions`` row, written by the ``race-postprocess`` Cloud Run
Job — see ``workers/race_postprocess.py``.

``POST /api/races/{race_id}/stats/regenerate`` — re-fire the Cloud
Run Job with ``--force``. Pro-tier gated. Used by the
"Regenerate summary" button on the stats view; intentionally not
on the free tier because every press is an Anthropic call.

Why the GET returns stats even when the summary is missing:
  * The Cloud Run Job may not have finished yet — first open of the
    stats page can race against the job. The view renders the stats
    immediately and shows a skeleton in the summary card.
  * The Anthropic call can fail (no key in dev, transient error in
    prod). Stats should never be hostage to the LLM.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import db, redis_client
from app.auth import get_current_user, require_pro
from app.auth_helpers import race_owner_predicate, race_read_predicate
from app.services.job_trigger import trigger_race_postprocess
from app.services.race_stats import (
    compute_stats,
    track_points_from_rows,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/races", tags=["race-stats"])


# Redis cache config. Key incorporates the track point count so that
# any new flush invalidates the cached stats automatically.
_CACHE_VERSION = 1
_CACHE_TTL_S = 3600


# ─── Response models ──────────────────────────────────────────────────


class LegOut(BaseModel):
    leg_index: int
    from_label: str
    to_label: str
    start_ts: str
    end_ts: str
    elapsed_s: float
    distance_m: float
    avg_sog_kt: float


class SpeedSampleOut(BaseModel):
    t_offset_s: float
    sog_kt: float


class StatsOut(BaseModel):
    point_count: int
    started_at: str
    ended_at: str
    elapsed_s: float
    moving_s: float
    stopped_s: float
    distance_m: float
    avg_sog_kt: float
    avg_moving_sog_kt: float
    max_sog_kt: float
    legs: list[LegOut]
    speed_series: list[SpeedSampleOut]
    # D2 corrected-time fields. All None when the race has no boat
    # set or the boat doesn't carry the relevant rating.
    corrected_time_s: Optional[float] = None
    corrected_using: Optional[str] = None
    rating_seconds_per_mile: Optional[int] = None


class BoatSummaryOut(BaseModel):
    """Just enough of the boat record to label the corrected-time tile
    and the stats view header. Full boat detail is at /api/boats/{id}."""
    id: UUID
    name: str
    sail_number: Optional[str] = None
    mwphrf_region: Optional[int] = None
    hcp: Optional[int] = None
    dhcp: Optional[int] = None
    nshcp: Optional[int] = None
    dnshcp: Optional[int] = None


class AiSummaryOut(BaseModel):
    recap: str
    tips: list[str]
    model: Optional[str] = None
    prompt_version: Optional[int] = None
    generated_at: Optional[str] = None


class WindSnapshotMetaOut(BaseModel):
    """Compact metadata about the wind snapshot — the full grid is
    available via a separate endpoint (or directly on the row when we
    add admin tooling). The stats view only needs the headline numbers
    for the wind card."""
    source: Optional[str] = None
    t_start: Optional[str] = None
    t_end: Optional[str] = None
    grid_deg: Optional[float] = None
    mean_speed_kt: Optional[float] = None
    max_speed_kt: Optional[float] = None
    mean_dir_deg: Optional[float] = None
    dir_range_deg: Optional[float] = None
    cell_coverage: Optional[float] = None


class StatsResponse(BaseModel):
    race_id: UUID
    name: Optional[str] = None
    boat_class: Optional[str] = None
    start_at: Optional[str] = None
    mode: Optional[str] = None                # "inshore" | "distance"
    uses_spinnaker: bool = True
    boat: Optional[BoatSummaryOut] = None
    # Echo the marks so the frontend's read-only map can render them
    # without a second /api/races/{id} round trip. Same shape the
    # editor stores: list of {lat, lon, name?, ...}. Empty list when
    # the race has no course set.
    marks: list[dict[str, Any]] = []
    stats: Optional[StatsOut] = None
    ai_summary: Optional[AiSummaryOut] = None
    wind: Optional[WindSnapshotMetaOut] = None
    # Hints for the frontend so it knows whether to poll for the job
    # to finish (no summary yet) or surface "regenerate" affordances.
    summary_pending: bool = False


class RegenerateAccepted(BaseModel):
    accepted: bool


# ─── Helpers ──────────────────────────────────────────────────────────


async def _load_race_row(
    conn: asyncpg.Connection, race_id: UUID, uid: str,
) -> dict:
    """Auth + load. 404 on missing-or-no-access (we don't leak existence).

    Read access: caller created the race OR is a member of the race's
    boat at ANY role (including viewer). LEFT JOIN the boats table so
    corrected time + boat summary come back in the same round trip.
    """
    pred = race_read_predicate(race_alias="r", uid_placeholder="$2")
    row = await conn.fetchrow(
        f"""
        SELECT
            r.id, r.name, r.boat_class, r.start_at, r.marks,
            r.mark_passes, r.ai_summary, r.wind_snapshot,
            r.mode, r.uses_spinnaker, r.boat_id,
            b.id           AS boat_pk,
            b.name         AS boat_name,
            b.sail_number  AS boat_sail_number,
            b.mwphrf_region AS boat_mwphrf_region,
            b.hcp          AS boat_hcp,
            b.dhcp         AS boat_dhcp,
            b.nshcp        AS boat_nshcp,
            b.dnshcp       AS boat_dnshcp
        FROM race_sessions r
        LEFT JOIN boats b ON b.id = r.boat_id
        WHERE r.id = $1 AND {pred}
        """,
        race_id, uid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")
    return dict(row)


async def _load_track_rows(
    conn: asyncpg.Connection, race_id: UUID,
) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT
            recorded_at,
            ST_Y(position::geometry) AS lat,
            ST_X(position::geometry) AS lon,
            speed_kts,
            heading_deg
        FROM track_points
        WHERE session_id = $1
        ORDER BY recorded_at ASC
        """,
        race_id,
    )
    return [dict(r) for r in rows]


def _cache_key(race_id: UUID, point_count: int, tier: Optional[str] = None) -> str:
    # Tier in the key so free vs pro cached responses don't collide.
    # None tier (unauthenticated callers can't reach this endpoint, but
    # defensive) folds into 'free'.
    t = tier or "free"
    return f"race_stats:{race_id}:{point_count}:t{t}:v{_CACHE_VERSION}"


async def _cached_stats(
    race_id: UUID, point_count: int, *, tier: Optional[str] = None,
) -> Optional[dict]:
    """Best-effort read of the cached stats dict. Returns None when
    Redis is unavailable or the key doesn't exist — caller falls back
    to recomputing."""
    try:
        client = redis_client.get_client()
    except HTTPException:
        return None
    try:
        raw = await client.get(_cache_key(race_id, point_count, tier))
    except Exception as e:  # noqa: BLE001
        log.warning("race_stats: cache read failed (%s)", e)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


async def _cache_stats(
    race_id: UUID, point_count: int, stats_dict: dict,
    *, tier: Optional[str] = None,
) -> None:
    try:
        client = redis_client.get_client()
    except HTTPException:
        return
    try:
        await client.setex(
            _cache_key(race_id, point_count, tier),
            _CACHE_TTL_S,
            json.dumps(stats_dict),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("race_stats: cache write failed (%s)", e)


def _build_wind_meta(snapshot: Optional[dict]) -> Optional[WindSnapshotMetaOut]:
    if not snapshot:
        return None
    # Late import to avoid touching wind_snapshot.py at module load
    # time (its top imports are cheap, but this keeps the router
    # boot-time graph minimal).
    from app.services.wind_snapshot import summarise_snapshot
    summ = summarise_snapshot(snapshot)
    return WindSnapshotMetaOut(
        source=snapshot.get("source"),
        t_start=snapshot.get("t_start"),
        t_end=snapshot.get("t_end"),
        grid_deg=snapshot.get("grid_deg"),
        mean_speed_kt=summ.get("mean_speed_kt"),
        max_speed_kt=summ.get("max_speed_kt"),
        mean_dir_deg=summ.get("mean_dir_deg"),
        dir_range_deg=summ.get("dir_range_deg"),
        cell_coverage=summ.get("cell_coverage"),
    )


# ─── GET ──────────────────────────────────────────────────────────────


@router.get("/{race_id}/stats", response_model=StatsResponse)
async def get_stats(
    race_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> StatsResponse:
    """Return computed stats + persisted AI summary + wind summary.

    Always returns 200 when the race exists, even if it has no track
    points or no AI summary yet. The frontend handles the partial
    payload (skeleton states for the missing pieces).
    """
    async with pool.acquire() as conn:
        race = await _load_race_row(conn, race_id, user["uid"])
        track_rows = await _load_track_rows(conn, race_id)

    # Pull the boat fields out of the joined row into a dict the
    # service layer recognises. None when the race has no boat_id.
    # D3 pro-tier gating: ratings are stripped from the response when
    # the CALLER is free-tier (`is_pro` computed below). The boat's
    # identity (name, sail #, region) is still surfaced.
    boat_for_math = None
    boat_summary = None
    is_pro_caller = user.get("tier") in ("pro", "hardware")
    if race.get("boat_pk") is not None:
        boat_for_math = {
            "hcp": race.get("boat_hcp"),
            "dhcp": race.get("boat_dhcp"),
            "nshcp": race.get("boat_nshcp"),
            "dnshcp": race.get("boat_dnshcp"),
        }
        boat_summary = BoatSummaryOut(
            id=race["boat_pk"],
            name=race.get("boat_name") or "",
            sail_number=race.get("boat_sail_number"),
            mwphrf_region=race.get("boat_mwphrf_region"),
            hcp=race.get("boat_hcp") if is_pro_caller else None,
            dhcp=race.get("boat_dhcp") if is_pro_caller else None,
            nshcp=race.get("boat_nshcp") if is_pro_caller else None,
            dnshcp=race.get("boat_dnshcp") if is_pro_caller else None,
        )

    # D3 pro-tier gating: free callers don't see corrected time.
    # The math runs only for pro+/hardware; for free we pass boat=None
    # so the service skips the corrected-time fields entirely.
    is_pro = user.get("tier") in ("pro", "hardware")
    boat_for_compute = boat_for_math if is_pro else None

    stats_dict: Optional[dict] = None
    if track_rows:
        point_count = len(track_rows)
        # Cache key intentionally does NOT include the boat — the
        # boat's rating affects only the corrected-time field, which
        # we re-derive on every read from the boat row. If we cached
        # corrected time together with elapsed, a rating edit would
        # leave stale data behind until the next track flush.
        # D3: cache key DOES include tier so a free → pro upgrade
        # surfaces the corrected time on next fetch.
        stats_dict = await _cached_stats(race_id, point_count, tier=user.get("tier"))
        if stats_dict is None:
            pts = track_points_from_rows(track_rows)
            computed = compute_stats(
                pts,
                marks=race.get("marks") or [],
                mark_passes=race.get("mark_passes") or [],
                race_start_at=race.get("start_at"),
                boat=boat_for_compute,
                mode=race.get("mode"),
                uses_spinnaker=bool(race.get("uses_spinnaker", True)),
            )
            if computed is not None:
                stats_dict = computed.to_dict()
                await _cache_stats(
                    race_id, point_count, stats_dict, tier=user.get("tier"),
                )

    ai_summary = race.get("ai_summary")
    wind = _build_wind_meta(race.get("wind_snapshot"))

    summary_pending = stats_dict is not None and ai_summary is None

    return StatsResponse(
        race_id=race_id,
        name=race.get("name"),
        boat_class=race.get("boat_class"),
        start_at=race["start_at"].isoformat() if race.get("start_at") else None,
        mode=race.get("mode"),
        uses_spinnaker=bool(race.get("uses_spinnaker", True)),
        boat=boat_summary,
        marks=race.get("marks") or [],
        stats=StatsOut(**stats_dict) if stats_dict else None,
        ai_summary=AiSummaryOut(**ai_summary) if ai_summary else None,
        wind=wind,
        summary_pending=summary_pending,
    )


# ─── POST /regenerate ─────────────────────────────────────────────────


@router.post(
    "/{race_id}/stats/regenerate",
    response_model=RegenerateAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_summary(
    race_id: UUID,
    user: dict = Depends(require_pro),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> RegenerateAccepted:
    """Pro-only: re-fire the Cloud Run Job with --force.

    Ownership is verified via the same SELECT used by GET (404 on
    miss). The trigger itself is fire-and-forget and the job is
    idempotent, so a double-click results in the same final state.
    """
    pred = race_owner_predicate(race_alias="r", uid_placeholder="$2")
    async with pool.acquire() as conn:
        # Auth check: owner-only (crew + viewer cannot trigger a paid
        # Anthropic call). require_pro above also gates by tier.
        owned = await conn.fetchrow(
            f"SELECT 1 FROM race_sessions r WHERE r.id = $1 AND {pred}",
            race_id, user["uid"],
        )
        if owned is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "race not found")

    await trigger_race_postprocess(race_id, force=True)
    return RegenerateAccepted(accepted=True)
