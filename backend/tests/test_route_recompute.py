# backend/tests/test_route_recompute.py
"""Tests for the route_recompute background worker.

Post Phase 2 the worker is a thin transport over
``app.services.routing.pipeline.compute_route``. These tests mock
``compute_route`` and cover the selection + notification logic:

* Empty candidate set → no-op.
* Pre-start race WITHOUT stored knobs → skipped (never computed = not
  eligible; that's the v12 "has a computed route" selection rule).
* Pre-start race with knobs → publishes kind="update" on every pass
  (v12: every ingest produces a visible route), kind="better" when the
  improvement clears the threshold.
* Baseline is phase-scoped JSON; legacy bare-float baselines parse as
  phase "pre".
* In-progress race → routes from the latest GPS fix to the remaining
  marks (mark_passes cursor), race_start ≈ now, use_cache=False.
* In-progress race with no fixes → full course fallback.
* In-progress race past its ETA window → skipped.
* Phase flip (pre → live) resets the improvement comparison.
* :class:`ForecastNotAvailable` from the pipeline → skipped quietly.
* Engine result with ``reached=False`` → nothing published.
* Stored user knobs are replayed onto the RouteRequest the worker
  passes to ``compute_route`` (faithful-replay contract).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.routing.pipeline import (
    DeratingProfile,
    RouteOutcome,
    RouteRequestKnobs,
)
from app.services.weather.forecast_loader import ForecastNotAvailable
from workers import route_recompute


# ─── Helpers ─────────────────────────────────────────────────────────────


_MARKS = [
    {"name": "Waukegan", "lat": 42.3636, "lon": -87.8261},
    {"name": "Chicago",  "lat": 41.8881, "lon": -87.6132},
]


def _race_row(*, start_delta_hours: float = 2.0, mark_passes: list | None = None):
    return {
        "id": uuid4(),
        "user_id": "test-uid",
        "boat_class": "Beneteau First 36.7",
        "marks": json.dumps(_MARKS),
        "start_at": datetime.now(timezone.utc) + timedelta(hours=start_delta_hours),
        "mark_passes": json.dumps(mark_passes or []),
    }


def _knobs_blob() -> bytes:
    return RouteRequestKnobs(
        safety_factor=1.25,
        duration_hours=48.0,
        derating=DeratingProfile(
            max_tws_kt=30.0,
            polar_margin=0.95,
            hs_m=0.5,
            density_factor=1.02,
        ),
    ).to_json()


def _baseline_blob(total_minutes: float, phase: str = "pre") -> bytes:
    return json.dumps({"total_minutes": total_minutes, "phase": phase}).encode()


def _outcome(total_minutes: float, reached: bool = True) -> RouteOutcome:
    return RouteOutcome(
        feature={"type": "Feature",
                 "geometry": {"type": "LineString", "coordinates": []},
                 "properties": {}},
        meta={
            "total_minutes": total_minutes,
            "tack_count": 1,
            "reached": reached,
            "iterations": 50,
            "nodes_explored": 1000,
            "legs": 1,
            "region": "conus",
        },
        cached=False,
        cache_key="route:v12-fullrace:test",
    )


def _fake_get(*, knobs: bytes | None = None, baseline: bytes | None = None):
    """Redis.get side_effect that branches on the key family."""
    async def fake_get(key):
        k = str(key)
        if "last_request" in k:
            return knobs
        if "last_best" in k:
            return baseline
        return None
    return fake_get


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    conn.fetchrow.return_value = None  # default: no GPS fixes uploaded
    return pool, conn


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.setex.return_value = True
    redis.publish.return_value = 1
    return redis


async def _recompute(pool, redis, compute_mock):
    """Patch the worker's I/O seams and run one full pass."""
    with patch("workers.route_recompute.db.get_pool",
               new=AsyncMock(return_value=pool)), \
         patch("workers.route_recompute.redis_client.get_client",
               return_value=redis), \
         patch("workers.route_recompute.compute_route", new=compute_mock):
        await route_recompute.recompute_all()


def _published_payload(mock_redis) -> dict:
    mock_redis.publish.assert_awaited_once()
    _channel, blob = mock_redis.publish.await_args.args
    return json.loads(blob)


# ─── Selection ───────────────────────────────────────────────────────────


async def test_no_candidate_races_is_noop(mock_pool, mock_redis):
    pool, conn = mock_pool
    conn.fetch.return_value = []

    compute_mock = AsyncMock(return_value=_outcome(420.0))
    await _recompute(pool, mock_redis, compute_mock)

    compute_mock.assert_not_awaited()
    mock_redis.publish.assert_not_called()


async def test_pre_start_without_stored_knobs_is_skipped(mock_pool, mock_redis):
    """v12 selection: pre-start races are eligible only once the user has
    computed a route (route:last_request exists). Never-computed races
    don't burn engine time on every ingest."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row(start_delta_hours=40.0)]
    mock_redis.get.side_effect = _fake_get(knobs=None)

    compute_mock = AsyncMock(return_value=_outcome(420.0))
    await _recompute(pool, mock_redis, compute_mock)

    compute_mock.assert_not_awaited()
    mock_redis.publish.assert_not_called()


async def test_live_race_past_eta_window_is_skipped(mock_pool, mock_redis):
    """Started 100h ago with a ~7h baseline → way past ETA × margin."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row(start_delta_hours=-100.0)]
    mock_redis.get.side_effect = _fake_get(
        knobs=_knobs_blob(), baseline=_baseline_blob(420.0, phase="live"),
    )

    compute_mock = AsyncMock(return_value=_outcome(400.0))
    await _recompute(pool, mock_redis, compute_mock)

    compute_mock.assert_not_awaited()
    mock_redis.publish.assert_not_called()


# ─── Pre-start publish semantics ─────────────────────────────────────────


async def test_first_pass_publishes_update_and_stores_baseline(
    mock_pool, mock_redis,
):
    """v12: no prior baseline → still publish (kind=update) so the map
    refreshes; baseline established for the next pass's comparison."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row()]
    mock_redis.get.side_effect = _fake_get(knobs=_knobs_blob(), baseline=None)

    await _recompute(pool, mock_redis, AsyncMock(return_value=_outcome(420.0)))

    payload = _published_payload(mock_redis)
    assert payload["kind"] == "update"
    assert payload["phase"] == "pre"
    assert payload["computed_from"] == "start"
    assert payload["new_total_minutes"] == 420.0
    assert payload["improvement_minutes"] == 0.0

    setex_calls = {
        str(call.args[0]): call.args[2] for call in mock_redis.setex.await_args_list
    }
    baseline_blobs = [
        v for k, v in setex_calls.items() if k.startswith("route:last_best:")
    ]
    assert len(baseline_blobs) == 1
    stored = json.loads(baseline_blobs[0])
    assert stored == {"total_minutes": 420.0, "phase": "pre"}


async def test_improvement_above_threshold_publishes_better(mock_pool, mock_redis):
    """Baseline 420 min, new 380 min → ~9.5% improvement → kind=better."""
    pool, conn = mock_pool
    race = _race_row()
    conn.fetch.return_value = [race]
    mock_redis.get.side_effect = _fake_get(
        knobs=_knobs_blob(), baseline=_baseline_blob(420.0, phase="pre"),
    )

    await _recompute(pool, mock_redis, AsyncMock(return_value=_outcome(380.0)))

    payload = _published_payload(mock_redis)
    channel, _blob = mock_redis.publish.await_args.args
    assert channel == f"route:notifications:{race['id']}"
    assert payload["kind"] == "better"
    assert payload["old_total_minutes"] == 420.0
    assert payload["new_total_minutes"] == 380.0
    assert payload["improvement_pct"] > 5.0


async def test_improvement_below_threshold_publishes_update(mock_pool, mock_redis):
    """Baseline 420, new 415 → ~1.2% → kind=update (map refresh, no banner)."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row()]
    mock_redis.get.side_effect = _fake_get(
        knobs=_knobs_blob(), baseline=_baseline_blob(420.0, phase="pre"),
    )

    await _recompute(pool, mock_redis, AsyncMock(return_value=_outcome(415.0)))

    payload = _published_payload(mock_redis)
    assert payload["kind"] == "update"
    assert payload["improvement_minutes"] == 0.0


async def test_legacy_bare_float_baseline_parses_as_pre_phase(
    mock_pool, mock_redis,
):
    """Pre-v12 workers stored b"420.0" — must compare, not re-baseline."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row()]
    mock_redis.get.side_effect = _fake_get(knobs=_knobs_blob(), baseline=b"420.0")

    await _recompute(pool, mock_redis, AsyncMock(return_value=_outcome(380.0)))

    payload = _published_payload(mock_redis)
    assert payload["kind"] == "better"
    assert payload["old_total_minutes"] == 420.0


async def test_pre_start_uses_cache(mock_pool, mock_redis):
    """Pre-start recompute warms the endpoint's route cache."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row()]
    mock_redis.get.side_effect = _fake_get(knobs=_knobs_blob())

    compute_mock = AsyncMock(return_value=_outcome(420.0))
    await _recompute(pool, mock_redis, compute_mock)

    assert compute_mock.await_args.kwargs["use_cache"] is True


# ─── In-progress (live) semantics ────────────────────────────────────────


async def test_live_race_routes_from_latest_fix_to_remaining_marks(
    mock_pool, mock_redis,
):
    """Started race with a fix → marks = [fix, *remaining], phase=live,
    race_start ≈ now, no cache."""
    pool, conn = mock_pool
    race = _race_row(start_delta_hours=-1.0, mark_passes=[
        {"mark_index": 0, "ts": "2026-07-09T15:00:00+00:00",
         "lat": 42.3636, "lon": -87.8261},
    ])
    conn.fetch.return_value = [race]
    conn.fetchrow.return_value = {
        "lat": 42.10, "lon": -87.70,
        "recorded_at": datetime.now(timezone.utc) - timedelta(minutes=1),
    }
    mock_redis.get.side_effect = _fake_get(
        knobs=_knobs_blob(), baseline=_baseline_blob(420.0, phase="live"),
    )

    compute_mock = AsyncMock(return_value=_outcome(180.0))
    await _recompute(pool, mock_redis, compute_mock)

    req = compute_mock.await_args.args[0]
    # Origin is the live fix, then only the un-rounded marks.
    assert req.marks[0] == {"lat": 42.10, "lon": -87.70}
    assert req.marks[1:] == _MARKS[1:]
    # Engine clock starts now, not at the gun.
    assert abs((req.race_start - datetime.now(timezone.utc)).total_seconds()) < 60
    assert compute_mock.await_args.kwargs["use_cache"] is False

    payload = _published_payload(mock_redis)
    assert payload["phase"] == "live"
    assert payload["computed_from"] == "live_position"


async def test_live_race_without_fixes_falls_back_to_full_course(
    mock_pool, mock_redis,
):
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row(start_delta_hours=-1.0)]
    conn.fetchrow.return_value = None  # no telemetry uploaded
    mock_redis.get.side_effect = _fake_get(
        knobs=_knobs_blob(), baseline=_baseline_blob(420.0, phase="live"),
    )

    compute_mock = AsyncMock(return_value=_outcome(400.0))
    await _recompute(pool, mock_redis, compute_mock)

    req = compute_mock.await_args.args[0]
    assert req.marks == _MARKS
    payload = _published_payload(mock_redis)
    assert payload["computed_from"] == "start"


async def test_live_race_without_knobs_still_recomputes(mock_pool, mock_redis):
    """Redis flush mid-race must not silence updates — library defaults."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row(start_delta_hours=-1.0)]
    mock_redis.get.side_effect = _fake_get(knobs=None, baseline=None)

    compute_mock = AsyncMock(return_value=_outcome(400.0))
    await _recompute(pool, mock_redis, compute_mock)

    compute_mock.assert_awaited_once()
    payload = _published_payload(mock_redis)
    assert payload["kind"] == "update"


async def test_phase_flip_resets_improvement_comparison(mock_pool, mock_redis):
    """Pre-start baseline 420, live remaining-course 180 → NOT 'better'
    (different denominators); publish update and re-baseline as live."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row(start_delta_hours=-1.0)]
    mock_redis.get.side_effect = _fake_get(
        knobs=_knobs_blob(), baseline=_baseline_blob(420.0, phase="pre"),
    )

    await _recompute(pool, mock_redis, AsyncMock(return_value=_outcome(180.0)))

    payload = _published_payload(mock_redis)
    assert payload["kind"] == "update"

    setex_calls = {
        str(call.args[0]): call.args[2] for call in mock_redis.setex.await_args_list
    }
    baseline_blobs = [
        v for k, v in setex_calls.items() if k.startswith("route:last_best:")
    ]
    assert json.loads(baseline_blobs[-1]) == {"total_minutes": 180.0, "phase": "live"}


async def test_live_race_all_marks_passed_is_skipped(mock_pool, mock_redis):
    """Every mark rounded (ended_at just hasn't landed) → nothing to route."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row(
        start_delta_hours=-1.0,
        mark_passes=[{"mark_index": i} for i in range(len(_MARKS))],
    )]
    mock_redis.get.side_effect = _fake_get(
        knobs=_knobs_blob(), baseline=_baseline_blob(420.0, phase="live"),
    )

    compute_mock = AsyncMock(return_value=_outcome(10.0))
    await _recompute(pool, mock_redis, compute_mock)

    compute_mock.assert_not_awaited()
    mock_redis.publish.assert_not_called()


# ─── Failure paths ───────────────────────────────────────────────────────


async def test_forecast_not_available_skips_race_quietly(mock_pool, mock_redis):
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row()]
    mock_redis.get.side_effect = _fake_get(knobs=_knobs_blob())

    compute_mock = AsyncMock(side_effect=ForecastNotAvailable(
        available_at=datetime.now(timezone.utc) + timedelta(hours=4),
    ))
    await _recompute(pool, mock_redis, compute_mock)

    mock_redis.publish.assert_not_called()
    setex_keys = [str(call.args[0]) for call in mock_redis.setex.await_args_list]
    assert not any(k.startswith("route:last_best:") for k in setex_keys)


async def test_engine_did_not_reach_finish_publishes_nothing(
    mock_pool, mock_redis,
):
    """reached=False → never surface a partial route (v12: this now means
    genuinely blocked, since the budget is sized to the course)."""
    pool, conn = mock_pool
    conn.fetch.return_value = [_race_row()]
    mock_redis.get.side_effect = _fake_get(
        knobs=_knobs_blob(), baseline=_baseline_blob(420.0, phase="pre"),
    )

    await _recompute(
        pool, mock_redis, AsyncMock(return_value=_outcome(200.0, reached=False)),
    )

    mock_redis.publish.assert_not_called()


# ─── Faithful replay ─────────────────────────────────────────────────────


async def test_stored_knobs_are_replayed_onto_request(mock_pool, mock_redis):
    """Faithful-replay contract: the worker reads route:last_request,
    decodes it, and the RouteRequest handed to compute_route carries
    the same safety_factor / derating the user chose."""
    pool, conn = mock_pool
    race = _race_row()
    conn.fetch.return_value = [race]
    mock_redis.get.side_effect = _fake_get(knobs=_knobs_blob())

    compute_mock = AsyncMock(return_value=_outcome(420.0))
    await _recompute(pool, mock_redis, compute_mock)

    compute_mock.assert_awaited_once()
    req = compute_mock.await_args.args[0]
    assert req.safety_factor == pytest.approx(1.25)
    assert req.duration_hours == pytest.approx(48.0)
    assert req.derating.max_tws_kt == pytest.approx(30.0)
    assert req.derating.polar_margin == pytest.approx(0.95)
    assert req.derating.hs_m == pytest.approx(0.5)
    assert req.derating.density_factor == pytest.approx(1.02)
    # Race-bound fields come from the DB row, not from the stored knobs.
    assert req.race_id == race["id"]
    assert req.boat_class == race["boat_class"]
    assert req.race_start == race["start_at"]
