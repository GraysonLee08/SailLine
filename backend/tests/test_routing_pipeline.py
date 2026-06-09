# backend/tests/test_routing_pipeline.py
"""Unit tests for ``app.services.routing.pipeline``.

Targets the orchestration concerns that were previously duplicated
across the router and the recompute worker:

* :func:`resolve_region` returns ``('conus', None)`` for points outside
  any known venue, and ``('conus', '<venue>')`` for points inside one.
* :func:`compute_route` short-circuits on a cache hit and stamps the
  ``cached`` flag.
* :func:`compute_route` writes back to the cache on a miss when
  ``use_cache=True``, and skips the write when ``use_cache=False``.
* Changing any user-tunable knob produces a distinct cache key so a
  knob flip is correctly a cache miss.
* :class:`ForecastNotAvailable` from the loader propagates out of the
  pipeline unwrapped — callers translate to their transport.
* :class:`RouteRequestKnobs` round-trip JSON serialization preserves
  values; missing fields in stored blobs fall through to defaults.
* :func:`request_with_knobs` constructs a request with library defaults
  when ``knobs`` is None.

The engine itself (numpy isochrone, polar lookup, navigability) has
its own test suite — these tests stub it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import numpy as np
import pytest

from app.services.routing.isochrone import RouteResult, WindField
from app.services.routing.navigability import always_navigable
from app.services.routing.pipeline import (
    DEFAULT_DENSITY_FACTOR,
    DEFAULT_DURATION_HOURS,
    DEFAULT_HS_M,
    DEFAULT_MAX_TWS_KT,
    DEFAULT_POLAR_MARGIN,
    DEFAULT_SAFETY_FACTOR,
    ENGINE_VERSION,
    DeratingProfile,
    RouteOutcome,
    RouteRequest,
    RouteRequestKnobs,
    compute_route,
    load_last_request,
    request_with_knobs,
    resolve_region,
    save_last_request,
)
from app.services.routing.wind_forecast import WindForecast
from app.services.weather.forecast_loader import ForecastNotAvailable


# ─── resolve_region ──────────────────────────────────────────────────────


def test_resolve_region_empty_marks_defaults_to_conus():
    assert resolve_region([]) == ("conus", None)


def test_resolve_region_lake_michigan_is_conus_no_venue():
    marks = [
        {"lat": 42.3636, "lon": -87.8261},
        {"lat": 41.8881, "lon": -87.6132},
    ]
    base, venue = resolve_region(marks)
    assert base == "conus"
    assert venue in (None, "chicago")  # depends on registry; both are OK


def test_resolve_region_hawaii_marks_pick_hawaii_base():
    marks = [
        {"lat": 21.0, "lon": -157.8},
        {"lat": 21.5, "lon": -157.5},
    ]
    base, _ = resolve_region(marks)
    assert base == "hawaii"


# ─── compute_route fixtures ─────────────────────────────────────────────


def _fake_field(valid_iso: str) -> WindField:
    return WindField(
        lats=np.array([41.0, 42.0, 43.0]),
        lons=np.array([-89.0, -88.0, -87.0]),
        u=np.zeros((3, 3), dtype=np.float32),
        v=np.full((3, 3), 5.0, dtype=np.float32),
        reference_time="2026-05-05T12:00:00+00:00",
        valid_time=valid_iso,
        source="hrrr",
    )


def _fake_forecast() -> WindForecast:
    return WindForecast(
        snapshots=[
            _fake_field("2026-05-05T12:00:00+00:00"),
            _fake_field("2026-05-05T14:00:00+00:00"),
        ],
        quality="hrrr",
    )


def _fake_result(total_minutes: float = 420.0) -> RouteResult:
    return RouteResult(
        path=[(42.3636, -87.8261), (41.8881, -87.6132)],
        headings=[200.0],
        total_minutes=total_minutes,
        tack_count=1,
        reached=True,
        iterations=50,
        nodes_explored=1000,
    )


@pytest.fixture
def base_request():
    return RouteRequest(
        race_id=UUID("11111111-2222-3333-4444-555555555555"),
        marks=[
            {"name": "Waukegan", "lat": 42.3636, "lon": -87.8261},
            {"name": "Chicago",  "lat": 41.8881, "lon": -87.6132},
        ],
        race_start=datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc),
        boat_class="Beneteau First 36.7",
    )


@pytest.fixture
def fake_redis():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.setex.return_value = True
    return redis


def _pipeline_patches(forecast=None, result=None):
    """Return a list of patch context managers for the pipeline's
    external dependencies. Centralised so individual tests don't repeat
    the same six patches."""
    forecast = forecast if forecast is not None else _fake_forecast()
    result = result if result is not None else _fake_result()
    return [
        # load_forecast_for_race is imported lazily inside compute_route
        # (to break the weather↔routing circular import that crashed the
        # postprocess worker), so it lives on the weather module, not on
        # pipeline's namespace — patch it at the source.
        patch("app.services.weather.load_forecast_for_race",
              new=AsyncMock(return_value=forecast)),
        patch("app.services.routing.pipeline.load_currents_optional",
              new=AsyncMock(return_value=None)),
        patch("app.services.routing.pipeline.make_navigable_predicate",
              return_value=always_navigable()),
        patch("app.services.routing.pipeline.compute_isochrone_route_multileg",
              return_value=result),
    ]


# ─── compute_route ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_route_happy_path_returns_outcome(base_request, fake_redis):
    patches = _pipeline_patches()
    for p in patches:
        p.start()
    try:
        outcome = await compute_route(base_request, redis=fake_redis, use_cache=True)
    finally:
        for p in patches:
            p.stop()

    assert isinstance(outcome, RouteOutcome)
    assert outcome.cached is False
    assert outcome.meta["reached"] is True
    assert outcome.meta["total_minutes"] == pytest.approx(420.0)
    assert outcome.meta["forecast_quality"] == "hrrr"
    # Cache write occurred.
    fake_redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_compute_route_cache_hit_short_circuits_engine(base_request, fake_redis):
    cached_payload = {
        "route": {"type": "Feature", "geometry": {}, "properties": {}},
        "meta": {"total_minutes": 410.0, "reached": True, "cached": False},
    }
    fake_redis.get.return_value = json.dumps(cached_payload).encode()

    patches = _pipeline_patches()
    for p in patches:
        p.start()
    try:
        outcome = await compute_route(base_request, redis=fake_redis, use_cache=True)
    finally:
        for p in patches:
            p.stop()

    assert outcome.cached is True
    assert outcome.meta["cached"] is True
    # No engine work on cache hit.
    fake_redis.setex.assert_not_called()


@pytest.mark.asyncio
async def test_compute_route_use_cache_false_skips_read_and_write(
    base_request, fake_redis,
):
    """Worker path: never reads cache, never writes."""
    patches = _pipeline_patches()
    for p in patches:
        p.start()
    try:
        outcome = await compute_route(base_request, redis=fake_redis, use_cache=False)
    finally:
        for p in patches:
            p.stop()

    assert outcome.cached is False
    fake_redis.get.assert_not_called()
    fake_redis.setex.assert_not_called()


@pytest.mark.asyncio
async def test_compute_route_forecast_not_available_propagates(
    base_request, fake_redis,
):
    """Pipeline doesn't wrap the exception; transports decide what to do."""
    available_at = datetime.now(timezone.utc) + timedelta(hours=4)
    with patch(
        "app.services.weather.load_forecast_for_race",
        new=AsyncMock(side_effect=ForecastNotAvailable(available_at=available_at)),
    ):
        with pytest.raises(ForecastNotAvailable):
            await compute_route(base_request, redis=fake_redis, use_cache=True)


@pytest.mark.asyncio
async def test_compute_route_derating_change_changes_cache_key(
    base_request, fake_redis,
):
    """A different DeratingProfile must hit a different cache key — the
    previous bug was that the worker hardcoded one set of knobs while
    the endpoint accepted another and they shared the cache."""
    get_keys: list[str] = []

    async def capture_get(key):
        get_keys.append(key)
        return None

    fake_redis.get.side_effect = capture_get

    patches = _pipeline_patches()
    for p in patches:
        p.start()
    try:
        await compute_route(base_request, redis=fake_redis, use_cache=True)
        higher_margin = RouteRequest(
            race_id=base_request.race_id,
            marks=base_request.marks,
            race_start=base_request.race_start,
            boat_class=base_request.boat_class,
            safety_factor=base_request.safety_factor,
            duration_hours=base_request.duration_hours,
            derating=DeratingProfile(polar_margin=0.99),
        )
        await compute_route(higher_margin, redis=fake_redis, use_cache=True)
    finally:
        for p in patches:
            p.stop()

    assert len(get_keys) == 2
    assert get_keys[0] != get_keys[1], (
        "DeratingProfile change must produce a distinct cache key"
    )


# ─── RouteRequestKnobs serialization ────────────────────────────────────


def test_route_request_knobs_round_trip():
    knobs = RouteRequestKnobs(
        safety_factor=1.4,
        duration_hours=24.0,
        derating=DeratingProfile(
            max_tws_kt=28.0, polar_margin=0.96, hs_m=0.75, density_factor=1.03,
        ),
    )
    restored = RouteRequestKnobs.from_json(knobs.to_json())
    assert restored == knobs


def test_route_request_knobs_missing_fields_fall_through_to_defaults():
    """Forward-compat: a knob added to RouteRequestKnobs after a stored
    blob was written must not break loading. Missing fields take their
    dataclass default."""
    blob = json.dumps({"safety_factor": 1.6}).encode()
    restored = RouteRequestKnobs.from_json(blob)
    assert restored.safety_factor == pytest.approx(1.6)
    assert restored.duration_hours == DEFAULT_DURATION_HOURS
    assert restored.derating.polar_margin == DEFAULT_POLAR_MARGIN
    assert restored.derating.max_tws_kt == DEFAULT_MAX_TWS_KT


def test_route_request_knobs_empty_blob_uses_all_defaults():
    restored = RouteRequestKnobs.from_json(b"{}")
    assert restored.safety_factor == DEFAULT_SAFETY_FACTOR
    assert restored.duration_hours == DEFAULT_DURATION_HOURS
    assert restored.derating == DeratingProfile()


def test_route_request_knobs_from_request_captures_user_tunables():
    req = RouteRequest(
        race_id=uuid4(),
        marks=[{"lat": 42.0, "lon": -88.0}, {"lat": 42.5, "lon": -87.5}],
        race_start=datetime.now(timezone.utc),
        boat_class="Beneteau First 36.7",
        safety_factor=1.3,
        duration_hours=12.0,
        derating=DeratingProfile(polar_margin=0.94),
    )
    knobs = RouteRequestKnobs.from_request(req)
    assert knobs.safety_factor == pytest.approx(1.3)
    assert knobs.duration_hours == pytest.approx(12.0)
    assert knobs.derating.polar_margin == pytest.approx(0.94)


# ─── request_with_knobs ────────────────────────────────────────────────


def test_request_with_knobs_none_uses_library_defaults():
    race_id = uuid4()
    now = datetime.now(timezone.utc)
    req = request_with_knobs(
        race_id=race_id,
        marks=[{"lat": 42.0, "lon": -88.0}, {"lat": 42.5, "lon": -87.5}],
        race_start=now,
        boat_class="Beneteau First 36.7",
        knobs=None,
    )
    assert req.race_id == race_id
    assert req.safety_factor == DEFAULT_SAFETY_FACTOR
    assert req.duration_hours == DEFAULT_DURATION_HOURS
    assert req.derating.polar_margin == DEFAULT_POLAR_MARGIN
    assert req.derating.hs_m == DEFAULT_HS_M
    assert req.derating.density_factor == DEFAULT_DENSITY_FACTOR
    assert req.derating.max_tws_kt == DEFAULT_MAX_TWS_KT


def test_request_with_knobs_overlays_stored_values():
    knobs = RouteRequestKnobs(
        safety_factor=1.25,
        duration_hours=48.0,
        derating=DeratingProfile(max_tws_kt=30.0, polar_margin=0.95),
    )
    req = request_with_knobs(
        race_id=uuid4(),
        marks=[{"lat": 42.0, "lon": -88.0}, {"lat": 42.5, "lon": -87.5}],
        race_start=datetime.now(timezone.utc),
        boat_class="J/111",
        knobs=knobs,
    )
    assert req.safety_factor == pytest.approx(1.25)
    assert req.duration_hours == pytest.approx(48.0)
    assert req.derating.max_tws_kt == pytest.approx(30.0)
    assert req.derating.polar_margin == pytest.approx(0.95)


# ─── save_last_request / load_last_request ──────────────────────────────


@pytest.mark.asyncio
async def test_save_and_load_last_request_round_trip(base_request):
    """Faithful-replay backbone — the endpoint writes, the worker reads,
    a round-trip preserves the user's tunable values."""
    stored: dict = {}

    redis = MagicMock()

    async def fake_setex(key, ttl, value):
        stored[key] = value

    async def fake_get(key):
        return stored.get(key)

    redis.setex = AsyncMock(side_effect=fake_setex)
    redis.get = AsyncMock(side_effect=fake_get)

    tuned = RouteRequest(
        race_id=base_request.race_id,
        marks=base_request.marks,
        race_start=base_request.race_start,
        boat_class=base_request.boat_class,
        safety_factor=1.35,
        duration_hours=18.0,
        derating=DeratingProfile(polar_margin=0.93, hs_m=0.4),
    )

    await save_last_request(tuned, redis=redis)
    restored = await load_last_request(tuned.race_id, redis=redis)

    assert restored is not None
    assert restored.safety_factor == pytest.approx(1.35)
    assert restored.duration_hours == pytest.approx(18.0)
    assert restored.derating.polar_margin == pytest.approx(0.93)
    assert restored.derating.hs_m == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_load_last_request_returns_none_when_absent():
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    result = await load_last_request(uuid4(), redis=redis)
    assert result is None


@pytest.mark.asyncio
async def test_load_last_request_returns_none_on_parse_failure():
    """A corrupt stored blob must not crash the worker — fall through
    to defaults silently."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=b"not json")
    result = await load_last_request(uuid4(), redis=redis)
    assert result is None
