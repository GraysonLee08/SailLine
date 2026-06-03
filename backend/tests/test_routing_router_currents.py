# backend/tests/test_routing_router_currents.py
"""Tests for the currents wiring in the routing pipeline.

Previously these tests reached into ``app.routers.routing`` for
``_currents_cache_tag`` and ``_load_currents_optional``. After the
Phase-2 pipeline collapse those helpers live in
``app.services.routing.pipeline`` — same behaviour, single home.
Tests don't exercise the full HTTP endpoint (that's covered by
``test_routing_router.py``); they target three currents-specific
behaviours of the pipeline:

  1. ``load_currents_optional`` swallows :class:`CurrentsUnavailable`
     and any other exception, returning ``None`` so the route still
     computes.
  2. ``_currents_cache_tag`` produces stable, distinct tags for the
     three states: no currents, currents present, currents-cycle
     change.
  3. The pinned engine version is what's documented in the pipeline
     module (sanity check — bump-without-test-update is a silent way
     to orphan cached routes).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.services.currents.fields import CurrentsUnavailable
from app.services.routing.pipeline import (
    ENGINE_VERSION,
    _currents_cache_tag,
    load_currents_optional,
)


# ─── ENGINE_VERSION sanity ──────────────────────────────────────────────


def test_engine_version_is_v11_pipeline():
    """v11 marks the pipeline collapse. Bumping this string forces a
    cache invalidation across every existing cached route — intentional."""
    assert ENGINE_VERSION == "v11-pipeline"


# ─── _currents_cache_tag ────────────────────────────────────────────────


def test_currents_cache_tag_none():
    assert _currents_cache_tag(None) == "none"


def test_currents_cache_tag_present_includes_quality_and_window():
    class _StubForecast:
        quality = "lmhofs"
        t_min = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
        t_max = datetime(2026, 5, 13, 18, tzinfo=timezone.utc)

    tag = _currents_cache_tag(_StubForecast())
    assert "lmhofs" in tag
    assert "2026-05-13T12:00:00+00:00" in tag
    assert "2026-05-13T18:00:00+00:00" in tag


def test_currents_cache_tag_changes_with_cycle():
    """Two different cycles of the same source produce distinct tags."""
    class _Stub:
        quality = "lmhofs"

        def __init__(self, hour):
            self.t_min = datetime(2026, 5, 13, hour, tzinfo=timezone.utc)
            self.t_max = self.t_min + timedelta(hours=6)

    assert _currents_cache_tag(_Stub(12)) != _currents_cache_tag(_Stub(18))


def test_currents_cache_tag_present_distinct_from_none():
    class _Stub:
        quality = "lmhofs"
        t_min = datetime(2026, 5, 13, 12, tzinfo=timezone.utc)
        t_max = datetime(2026, 5, 13, 18, tzinfo=timezone.utc)

    assert _currents_cache_tag(_Stub()) != _currents_cache_tag(None)


# ─── load_currents_optional ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_currents_optional_no_sources_returns_none():
    """No OFS source covers the marks bbox → None, no exception."""
    marks = [{"lat": 21.0, "lon": -157.8}, {"lat": 21.5, "lon": -157.5}]  # Hawaii
    result = await load_currents_optional(
        marks=marks,
        race_start=datetime(2026, 5, 13, 12, tzinfo=timezone.utc),
        duration_hours=4.0,
        race_id=uuid4(),
    )
    assert result is None


@pytest.mark.asyncio
async def test_load_currents_optional_swallows_currents_unavailable():
    """CurrentsUnavailable from the loader → None at the pipeline edge."""
    marks = [{"lat": 41.9, "lon": -87.6}, {"lat": 45.85, "lon": -84.62}]  # Mac course

    async def raise_unavailable(**kwargs):
        raise CurrentsUnavailable(["lmhofs"], "no cycle ingested yet")

    with patch(
        "app.services.routing.pipeline.load_currents_for_race",
        side_effect=raise_unavailable,
    ):
        result = await load_currents_optional(
            marks=marks,
            race_start=datetime(2026, 5, 13, 12, tzinfo=timezone.utc),
            duration_hours=4.0,
            race_id=uuid4(),
        )
    assert result is None


@pytest.mark.asyncio
async def test_load_currents_optional_swallows_arbitrary_exception():
    """A surprise exception in the loader must not fail the route compute."""
    marks = [{"lat": 41.9, "lon": -87.6}, {"lat": 45.85, "lon": -84.62}]

    async def raise_runtime(**kwargs):
        raise RuntimeError("redis blip")

    with patch(
        "app.services.routing.pipeline.load_currents_for_race",
        side_effect=raise_runtime,
    ):
        result = await load_currents_optional(
            marks=marks,
            race_start=datetime(2026, 5, 13, 12, tzinfo=timezone.utc),
            duration_hours=4.0,
            race_id=uuid4(),
        )
    assert result is None


@pytest.mark.asyncio
async def test_load_currents_optional_returns_forecast_on_success():
    """Happy path: loader returns a CurrentForecast → pipeline gets it."""
    marks = [{"lat": 41.9, "lon": -87.6}, {"lat": 45.85, "lon": -84.62}]
    sentinel = object()  # stand-in CurrentForecast — pipeline doesn't introspect

    async def return_sentinel(**kwargs):
        return sentinel

    with patch(
        "app.services.routing.pipeline.load_currents_for_race",
        side_effect=return_sentinel,
    ):
        result = await load_currents_optional(
            marks=marks,
            race_start=datetime(2026, 5, 13, 12, tzinfo=timezone.utc),
            duration_hours=4.0,
            race_id=uuid4(),
        )
    assert result is sentinel
