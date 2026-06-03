# backend/tests/test_routing_router.py
"""HTTP transport tests for /api/routing/compute.

Post Phase 2 the router is a thin shell over
``app.services.routing.pipeline.compute_route``. These tests mock
``compute_route`` itself and verify the HTTP-shaped concerns:

* 200 with the pipeline's RouteOutcome marshaled into the response.
* 425 (Too Early) when the pipeline raises :class:`ForecastNotAvailable`.
* 503 when the pipeline raises :class:`BathymetryUnavailable` or
  :class:`RuntimeError`.
* 400 when the race has fewer than 2 marks (pre-pipeline guard).
* 404 when the race is not owned by the caller.
* Race-start fallback to ``now()`` when ``start_at`` is NULL.
* The user's :class:`RouteRequest` is persisted via ``save_last_request``
  on success — this is the wire that lets the background recompute
  worker replay user knobs.

The pipeline's own internals (region resolution, forecast loading,
engine call, cache key) are covered by ``test_routing_pipeline.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import routing as routing_module
from app.services.bathymetry import BathymetryUnavailable
from app.services.routing.pipeline import RouteOutcome
from app.services.weather.forecast_loader import ForecastNotAvailable


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def waukegan_chicago_marks():
    return [
        {"name": "Waukegan", "lat": 42.3636, "lon": -87.8261},
        {"name": "Chicago",  "lat": 41.8881, "lon": -87.6132},
    ]


@pytest.fixture
def fake_outcome():
    """Stand-in for what ``compute_route`` returns. Real pipeline tests
    cover the field set; here we only need enough for the HTTP marshal."""
    return RouteOutcome(
        feature={
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[-87.83, 42.36], [-87.61, 41.89]]},
            "properties": {"region": "conus"},
        },
        meta={
            "total_minutes": 420.0,
            "tack_count": 2,
            "reached": True,
            "iterations": 84,
            "nodes_explored": 2400,
            "legs": 1,
            "region": "conus",
            "venue": None,
            "forecast_quality": "hrrr",
            "race_start": "2026-05-05T13:00:00+00:00",
            "polar": "beneteau_36_7",
            "boat_class": "Beneteau First 36.7",
            "draft_m": 2.05,
            "min_depth_m": 3.075,
            "cached": False,
            "max_tws_kt": None,
            "polar_margin": 0.97,
            "hs_m": 0.0,
            "density_factor": 1.0,
            "currents_quality": None,
            "start_wind_dir_deg": 180.0,
            "start_wind_speed_kt": 10.0,
        },
        cached=False,
        cache_key="route:v11-pipeline:irrelevant",
    )


@pytest.fixture
def race_row(waukegan_chicago_marks):
    return {
        "id": uuid4(),
        "marks": json.dumps(waukegan_chicago_marks),
        "boat_class": "Beneteau First 36.7",
        "start_at": datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc),
    }


@pytest.fixture
def mock_conn():
    return AsyncMock()


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.setex.return_value = True
    return redis


@pytest.fixture
def client(mock_conn, mock_redis):
    """TestClient with auth, db, and redis dependency overrides."""
    from app import db, redis_client
    from app.auth import get_current_user

    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = mock_conn
    pool.acquire.return_value.__aexit__.return_value = None

    app.dependency_overrides[get_current_user] = lambda: {"uid": "test-uid"}
    app.dependency_overrides[db.get_pool] = lambda: pool

    with patch.object(redis_client, "get_client", return_value=mock_redis):
        yield TestClient(app)

    app.dependency_overrides.clear()


# ─── Tests ───────────────────────────────────────────────────────────────


def test_compute_route_happy_path(client, mock_conn, race_row, fake_outcome):
    mock_conn.fetchrow.return_value = race_row

    with patch.object(
        routing_module, "compute_route",
        new=AsyncMock(return_value=fake_outcome),
    ) as mock_compute, patch.object(
        routing_module, "save_last_request",
        new=AsyncMock(return_value=None),
    ) as mock_save:
        r = client.post("/api/routing/compute",
                        json={"race_id": str(race_row["id"])})

    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["reached"] is True
    assert body["meta"]["forecast_quality"] == "hrrr"
    assert body["meta"]["region"] == "conus"
    assert body["route"]["type"] == "Feature"

    # Pipeline was called once; save_last_request was called once with
    # the same RouteRequest. The persisted request is what the worker
    # replays — the wiring matters.
    mock_compute.assert_awaited_once()
    mock_save.assert_awaited_once()


def test_compute_route_returns_425_when_forecast_pending(client, mock_conn, race_row):
    mock_conn.fetchrow.return_value = race_row
    available_at = datetime.now(timezone.utc) + timedelta(hours=6)

    with patch.object(
        routing_module, "compute_route",
        new=AsyncMock(side_effect=ForecastNotAvailable(
            available_at=available_at,
            reason="race starts beyond HRRR forecast horizon",
        )),
    ):
        r = client.post("/api/routing/compute",
                        json={"race_id": str(race_row["id"])})

    assert r.status_code == 425
    body = r.json()
    # FastAPI wraps detail dict under "detail"
    assert "available_at" in body["detail"]
    assert body["detail"]["hours_until_available"] > 0


def test_compute_route_returns_503_when_bathymetry_unavailable(
    client, mock_conn, race_row,
):
    mock_conn.fetchrow.return_value = race_row

    with patch.object(
        routing_module, "compute_route",
        new=AsyncMock(side_effect=BathymetryUnavailable("no grid for region=conus")),
    ):
        r = client.post("/api/routing/compute",
                        json={"race_id": str(race_row["id"])})
    assert r.status_code == 503
    assert "bathymetry_ingest" in r.json()["detail"]


def test_compute_route_returns_503_on_pipeline_runtime_error(
    client, mock_conn, race_row,
):
    mock_conn.fetchrow.return_value = race_row

    with patch.object(
        routing_module, "compute_route",
        new=AsyncMock(side_effect=RuntimeError("no ingested cycles")),
    ):
        r = client.post("/api/routing/compute",
                        json={"race_id": str(race_row["id"])})
    assert r.status_code == 503


def test_compute_route_404_when_race_not_owned(client, mock_conn):
    mock_conn.fetchrow.return_value = None
    r = client.post("/api/routing/compute",
                    json={"race_id": str(uuid4())})
    assert r.status_code == 404


def test_compute_route_400_when_fewer_than_two_marks(client, mock_conn):
    mock_conn.fetchrow.return_value = {
        "id": uuid4(),
        "marks": json.dumps([{"name": "only", "lat": 42.0, "lon": -88.0}]),
        "boat_class": "Beneteau First 36.7",
        "start_at": None,
    }
    r = client.post("/api/routing/compute", json={"race_id": str(uuid4())})
    assert r.status_code == 400
    assert "at least 2 marks" in r.json()["detail"]


def test_compute_route_falls_back_to_now_when_start_at_null(
    client, mock_conn, waukegan_chicago_marks, fake_outcome,
):
    """User computing a route on a race with no scheduled gun time."""
    mock_conn.fetchrow.return_value = {
        "id": uuid4(),
        "marks": json.dumps(waukegan_chicago_marks),
        "boat_class": "Beneteau First 36.7",
        "start_at": None,
    }
    with patch.object(
        routing_module, "compute_route",
        new=AsyncMock(return_value=fake_outcome),
    ) as mock_compute, patch.object(
        routing_module, "save_last_request",
        new=AsyncMock(return_value=None),
    ):
        r = client.post("/api/routing/compute", json={"race_id": str(uuid4())})

    assert r.status_code == 200
    # First positional arg to compute_route is the RouteRequest.
    req = mock_compute.await_args.args[0]
    delta = abs((req.race_start - datetime.now(timezone.utc)).total_seconds())
    assert delta < 5  # called within 5 seconds of now


def test_compute_route_passes_user_knobs_through_to_pipeline(
    client, mock_conn, race_row, fake_outcome,
):
    """Body parameters should reach the pipeline as a RouteRequest with
    a populated DeratingProfile. This is the contract that prevents the
    Phase-1 drift bug from re-emerging."""
    mock_conn.fetchrow.return_value = race_row

    with patch.object(
        routing_module, "compute_route",
        new=AsyncMock(return_value=fake_outcome),
    ) as mock_compute, patch.object(
        routing_module, "save_last_request",
        new=AsyncMock(return_value=None),
    ):
        r = client.post(
            "/api/routing/compute",
            json={
                "race_id": str(race_row["id"]),
                "safety_factor": 1.25,
                "duration_hours": 48.0,
                "max_tws_kt": 30.0,
                "polar_margin": 0.95,
                "hs_m": 0.5,
                "density_factor": 1.02,
            },
        )

    assert r.status_code == 200
    req = mock_compute.await_args.args[0]
    assert req.safety_factor == pytest.approx(1.25)
    assert req.duration_hours == pytest.approx(48.0)
    assert req.derating.max_tws_kt == pytest.approx(30.0)
    assert req.derating.polar_margin == pytest.approx(0.95)
    assert req.derating.hs_m == pytest.approx(0.5)
    assert req.derating.density_factor == pytest.approx(1.02)
