"""Tests for app/routers/race_stats.py.

Pattern follows test_tracks_router.py: override the FastAPI dependencies
for db.get_pool and get_current_user, stub asyncpg with AsyncMock. No
real DB.

Coverage:
  * 404 when race not owned (both GET and POST)
  * 200 with stats=None when no track points
  * 200 with stats computed when track exists
  * AI summary echoed when row has one; summary_pending=True otherwise
  * Wind summary derived from wind_snapshot when present
  * POST regenerate: 403 for free tier, 202 + trigger fired for pro
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.auth import get_current_user
from app.routers import race_stats


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def free_user():
    return {"uid": "test-uid", "email": "t@x", "tier": "free", "claims": {}}


@pytest.fixture
def pro_user():
    return {"uid": "test-uid", "email": "t@x", "tier": "pro", "claims": {}}


@pytest.fixture
def mock_conn():
    return AsyncMock()


def _make_app(user, mock_conn):
    @asynccontextmanager
    async def fake_acquire():
        yield mock_conn

    pool = MagicMock()
    pool.acquire = fake_acquire

    app = FastAPI()
    app.include_router(race_stats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[db.get_pool] = lambda: pool
    return app


@pytest.fixture
def free_client(free_user, mock_conn):
    return TestClient(_make_app(free_user, mock_conn))


@pytest.fixture
def pro_client(pro_user, mock_conn):
    return TestClient(_make_app(pro_user, mock_conn))


# ─── Synthetic data ───────────────────────────────────────────────────


T0 = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)


def _race_row(
    *,
    name="Test race",
    marks=None,
    mark_passes=None,
    ai_summary=None,
    wind_snapshot=None,
    boat=None,
    mode="inshore",
    uses_spinnaker=True,
):
    row = {
        "id": uuid4(),
        "name": name,
        "boat_class": "J/70",
        "start_at": T0,
        "marks": marks or [],
        "mark_passes": mark_passes or [],
        "ai_summary": ai_summary,
        "wind_snapshot": wind_snapshot,
        "mode": mode,
        "uses_spinnaker": uses_spinnaker,
        "boat_id": None,
        # LEFT JOIN columns — all None when no boat attached.
        "boat_pk": None,
        "boat_name": None,
        "boat_sail_number": None,
        "boat_mwphrf_region": None,
        "boat_hcp": None,
        "boat_dhcp": None,
        "boat_nshcp": None,
        "boat_dnshcp": None,
    }
    if boat:
        row["boat_id"] = boat.get("id") or uuid4()
        row["boat_pk"] = row["boat_id"]
        row["boat_name"] = boat.get("name", "Test boat")
        row["boat_sail_number"] = boat.get("sail_number")
        row["boat_mwphrf_region"] = boat.get("mwphrf_region")
        row["boat_hcp"] = boat.get("hcp")
        row["boat_dhcp"] = boat.get("dhcp")
        row["boat_nshcp"] = boat.get("nshcp")
        row["boat_dnshcp"] = boat.get("dnshcp")
    return row


def _track_rows(n=10):
    return [
        {
            "recorded_at": T0 + timedelta(seconds=i),
            "lat": 42.05,
            "lon": -87.75 + i * 0.00005,
            "speed_kts": 5.0,
            "heading_deg": 90.0,
        }
        for i in range(n)
    ]


def _setup_fetches(mock_conn, race_row, track_rows):
    """asyncpg fetchrow → race_row, fetch → track_rows."""
    mock_conn.fetchrow.return_value = race_row
    mock_conn.fetch.return_value = track_rows


# Stub the Redis cache helpers — keep tests independent of Memorystore.
@pytest.fixture(autouse=True)
def _stub_redis(monkeypatch):
    async def no_cache(*a, **kw): return None
    async def no_write(*a, **kw): return None
    monkeypatch.setattr(race_stats, "_cached_stats", no_cache)
    monkeypatch.setattr(race_stats, "_cache_stats", no_write)


# ─── GET ─────────────────────────────────────────────────────────────


def test_get_404_when_race_not_owned(free_client, mock_conn):
    mock_conn.fetchrow.return_value = None
    r = free_client.get(f"/api/races/{uuid4()}/stats")
    assert r.status_code == 404


def test_get_returns_200_with_stats_none_when_no_track(free_client, mock_conn):
    _setup_fetches(mock_conn, _race_row(), [])
    r = free_client.get(f"/api/races/{uuid4()}/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"] is None
    assert body["ai_summary"] is None
    # summary_pending is false when there's no track at all — nothing to summarise.
    assert body["summary_pending"] is False


def test_get_computes_stats_when_track_present(free_client, mock_conn):
    _setup_fetches(mock_conn, _race_row(), _track_rows(60))
    r = free_client.get(f"/api/races/{uuid4()}/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"] is not None
    assert body["stats"]["point_count"] == 60
    assert body["stats"]["elapsed_s"] == pytest.approx(59.0, abs=0.1)
    # No AI summary on the row → pending flag set.
    assert body["ai_summary"] is None
    assert body["summary_pending"] is True


def test_get_echoes_persisted_summary(free_client, mock_conn):
    summary = {
        "recap": "Solid race.",
        "tips": ["Trim earlier."],
        "model": "test",
        "prompt_version": 1,
        "generated_at": T0.isoformat(),
    }
    _setup_fetches(
        mock_conn, _race_row(ai_summary=summary), _track_rows(30)
    )
    r = free_client.get(f"/api/races/{uuid4()}/stats")
    body = r.json()
    assert body["ai_summary"]["recap"] == "Solid race."
    assert body["ai_summary"]["tips"] == ["Trim earlier."]
    assert body["summary_pending"] is False


def test_get_returns_wind_meta_when_snapshot_present(free_client, mock_conn):
    snap = {
        "bbox": [42.0, -87.7, 42.1, -87.5],
        "grid_deg": 0.1,
        "lats": [42.0, 42.1],
        "lons": [-87.7, -87.5],
        "t_start": T0.isoformat(),
        "t_end": (T0 + timedelta(minutes=30)).isoformat(),
        "dt_s": 900,
        "times": [T0.isoformat()],
        "source": "hybrid",
        "u_mps": [[[5.144, 5.144], [5.144, 5.144]]],
        "v_mps": [[[0.0, 0.0], [0.0, 0.0]]],
    }
    _setup_fetches(mock_conn, _race_row(wind_snapshot=snap), _track_rows(10))
    r = free_client.get(f"/api/races/{uuid4()}/stats")
    body = r.json()
    assert body["wind"] is not None
    assert body["wind"]["mean_speed_kt"] == pytest.approx(10.0, abs=0.1)
    assert body["wind"]["source"] == "hybrid"


def test_get_wind_meta_none_when_snapshot_absent(free_client, mock_conn):
    _setup_fetches(mock_conn, _race_row(wind_snapshot=None), _track_rows(10))
    r = free_client.get(f"/api/races/{uuid4()}/stats")
    body = r.json()
    assert body["wind"] is None


def test_get_includes_corrected_time_when_boat_has_rating(pro_client, mock_conn):
    # D3: corrected time is pro-tier only, so this happy-path test
    # now uses pro_client. The free-tier behaviour is exercised in
    # test_get_free_caller_does_not_see_corrected_time below.
    boat = {"name": "Gaucho", "hcp": 75, "dhcp": 78, "mwphrf_region": 5}
    _setup_fetches(
        mock_conn,
        _race_row(boat=boat, mode="inshore", uses_spinnaker=True),
        _track_rows(60),
    )
    r = pro_client.get(f"/api/races/{uuid4()}/stats")
    body = r.json()
    assert body["boat"] is not None
    assert body["boat"]["hcp"] == 75
    assert body["stats"]["corrected_using"] == "hcp"
    assert body["stats"]["rating_seconds_per_mile"] == 75
    # corrected_time_s should be a finite number, less than elapsed_s
    assert isinstance(body["stats"]["corrected_time_s"], (int, float))


def test_get_omits_boat_when_race_has_no_boat(free_client, mock_conn):
    _setup_fetches(mock_conn, _race_row(boat=None), _track_rows(10))
    r = free_client.get(f"/api/races/{uuid4()}/stats")
    body = r.json()
    assert body["boat"] is None
    assert body["stats"]["corrected_time_s"] is None


# ─── D3 pro-tier gating ───────────────────────────────────────────────


def test_get_free_caller_does_not_see_corrected_time(free_client, mock_conn):
    boat = {"name": "Gaucho", "hcp": 75, "dhcp": 78}
    _setup_fetches(
        mock_conn,
        _race_row(boat=boat, mode="inshore", uses_spinnaker=True),
        _track_rows(60),
    )
    r = free_client.get(f"/api/races/{uuid4()}/stats")
    body = r.json()
    # Boat is still in response (free can see boat identity) but
    # ratings are stripped and corrected time is None.
    assert body["boat"] is not None
    assert body["boat"]["hcp"] is None
    assert body["boat"]["dhcp"] is None
    assert body["stats"]["corrected_time_s"] is None
    assert body["stats"]["corrected_using"] is None


def test_get_pro_caller_sees_corrected_time(pro_client, mock_conn):
    boat = {"name": "Gaucho", "hcp": 75, "dhcp": 78}
    _setup_fetches(
        mock_conn,
        _race_row(boat=boat, mode="inshore", uses_spinnaker=True),
        _track_rows(60),
    )
    r = pro_client.get(f"/api/races/{uuid4()}/stats")
    body = r.json()
    assert body["boat"]["hcp"] == 75
    assert body["stats"]["corrected_using"] == "hcp"
    assert body["stats"]["rating_seconds_per_mile"] == 75


# ─── POST /regenerate ────────────────────────────────────────────────


def test_regenerate_403_for_free_tier(free_client, mock_conn):
    r = free_client.post(f"/api/races/{uuid4()}/stats/regenerate")
    assert r.status_code == 403


def test_regenerate_404_when_race_not_owned(pro_client, mock_conn, monkeypatch):
    fake_trigger = AsyncMock()
    monkeypatch.setattr(race_stats, "trigger_race_postprocess", fake_trigger)
    mock_conn.fetchrow.return_value = None
    r = pro_client.post(f"/api/races/{uuid4()}/stats/regenerate")
    assert r.status_code == 404
    fake_trigger.assert_not_awaited()


def test_regenerate_202_and_triggers_job(pro_client, mock_conn, monkeypatch):
    fake_trigger = AsyncMock()
    monkeypatch.setattr(race_stats, "trigger_race_postprocess", fake_trigger)
    mock_conn.fetchrow.return_value = {"?column?": 1}
    r = pro_client.post(f"/api/races/{uuid4()}/stats/regenerate")
    assert r.status_code == 202
    assert r.json() == {"accepted": True}
    fake_trigger.assert_awaited_once()
    # ``force=True`` kwarg passed through.
    _, kwargs = fake_trigger.await_args
    assert kwargs.get("force") is True
