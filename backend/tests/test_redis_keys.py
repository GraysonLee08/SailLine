# backend/tests/test_redis_keys.py
"""Fingerprint tests for ``app.services.redis_keys``.

Every builder is invoked with fixed inputs and the output string is
asserted verbatim. Pins the on-wire format so a key-shape change is a
unit-test failure rather than a silent cache miss in prod.

Add a fingerprint here when you add a key. If you intentionally change
a key shape, update the fingerprint AND bump the consumer version
constant (e.g. ``ENGINE_VERSION`` for the route cache) so cached
entries written under the old shape can't be misread.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.services import redis_keys


# ---------------------------------------------------------------------------
# Weather


def test_weather_fhour_key_fingerprint():
    assert (
        redis_keys.weather_fhour_key("hrrr", "conus", "20260603T1200Z", 6)
        == "weather:hrrr:conus:20260603T1200Z:f006"
    )


def test_weather_fhour_key_pads_to_3():
    """F18 (HRRR max) must come out as ``f018`` not ``f18``. Forecast loader
    parses by exact-string lookup; any width drift orphans the snapshot."""
    assert redis_keys.weather_fhour_key("hrrr", "conus", "C", 18).endswith("f018")
    assert redis_keys.weather_fhour_key("gfs", "conus", "C", 120).endswith("f120")
    assert redis_keys.weather_fhour_key("gfs", "conus", "C", 0).endswith("f000")


def test_weather_manifest_key_fingerprint():
    assert (
        redis_keys.weather_manifest_key("gfs", "conus", "20260603T0600Z")
        == "weather:gfs:conus:20260603T0600Z:manifest"
    )


def test_weather_cycles_index_key_fingerprint():
    assert (
        redis_keys.weather_cycles_index_key("hrrr", "conus")
        == "weather:hrrr:conus:cycles"
    )


def test_weather_latest_alias_key_fingerprint():
    """Backwards-compat /api/weather contract — must stay exactly this shape
    for the frontend's fetch to keep hitting cache."""
    assert (
        redis_keys.weather_latest_alias_key("hrrr", "conus")
        == "weather:hrrr:conus:latest"
    )


# ---------------------------------------------------------------------------
# Currents


def test_currents_topology_key_fingerprint():
    assert (
        redis_keys.currents_topology_key("lmhofs")
        == "currents:lmhofs:topology"
    )


def test_currents_snapshot_key_forecast_fingerprint():
    assert (
        redis_keys.currents_snapshot_key("lmhofs", "20260603T0900Z", "f", 6)
        == "currents:lmhofs:20260603T0900Z:f006"
    )


def test_currents_snapshot_key_nowcast_fingerprint():
    assert (
        redis_keys.currents_snapshot_key("lmhofs", "20260603T0900Z", "n", 3)
        == "currents:lmhofs:20260603T0900Z:n003"
    )


def test_currents_manifest_key_fingerprint():
    assert (
        redis_keys.currents_manifest_key("lmhofs", "20260603T0900Z", "f")
        == "currents:lmhofs:20260603T0900Z:f_manifest"
    )
    assert (
        redis_keys.currents_manifest_key("lmhofs", "20260603T0900Z", "n")
        == "currents:lmhofs:20260603T0900Z:n_manifest"
    )


def test_currents_cycles_index_key_fingerprint():
    assert (
        redis_keys.currents_cycles_index_key("lmhofs")
        == "currents:lmhofs:cycles"
    )


# ---------------------------------------------------------------------------
# Route compute + notifications


# Fixed inputs so the fingerprint is reproducible. Don't parametrize —
# the whole point is one canonical example per builder.
_ENGINE_VERSION = "v10-currents"
_RACE_ID = UUID("11111111-2222-3333-4444-555555555555")
_RACE_START = datetime(2026, 6, 3, 14, 0, 0, tzinfo=timezone.utc)


def test_route_cache_key_fingerprint_no_venue_no_currents():
    """Reference shape: no venue overlay, no currents folded in. The
    ``venue=-`` and ``currents=none`` substrings make 'absent' a distinct
    state from any present value — same shape as the in-place f-string it
    replaces.
    """
    key = redis_keys.route_cache_key(
        engine_version=_ENGINE_VERSION,
        race_id=_RACE_ID,
        race_start=_RACE_START,
        first_snapshot_ref="2026-06-03T12:00:00+00:00",
        last_snapshot_valid="2026-06-03T20:00:00+00:00",
        snapshot_sources="hrrr",
        safety_factor=1.50,
        venue=None,
        derating_tag="hs=0.00:dens=1.000:margin=0.970:cutoff=-",
        currents_tag="none",
    )
    assert key == (
        "route:v10-currents:11111111-2222-3333-4444-555555555555:"
        "2026-06-03T14:00:00+00:00:"
        "2026-06-03T12:00:00+00:00:2026-06-03T20:00:00+00:00:"
        "hrrr:1.50:venue=-:"
        "hs=0.00:dens=1.000:margin=0.970:cutoff=-:currents=none"
    )


def test_route_cache_key_fingerprint_with_venue_and_currents():
    key = redis_keys.route_cache_key(
        engine_version=_ENGINE_VERSION,
        race_id=_RACE_ID,
        race_start=_RACE_START,
        first_snapshot_ref="2026-06-03T12:00:00+00:00",
        last_snapshot_valid="2026-06-03T20:00:00+00:00",
        snapshot_sources="hrrr+gfs",
        safety_factor=1.25,
        venue="chicago",
        derating_tag="hs=0.50:dens=1.020:margin=0.950:cutoff=30",
        currents_tag="lmhofs:2026-06-03T09:00:00+00:00:2026-06-03T15:00:00+00:00",
    )
    assert key == (
        "route:v10-currents:11111111-2222-3333-4444-555555555555:"
        "2026-06-03T14:00:00+00:00:"
        "2026-06-03T12:00:00+00:00:2026-06-03T20:00:00+00:00:"
        "hrrr+gfs:1.25:venue=chicago:"
        "hs=0.50:dens=1.020:margin=0.950:cutoff=30:"
        "currents=lmhofs:2026-06-03T09:00:00+00:00:2026-06-03T15:00:00+00:00"
    )


def test_route_cache_key_safety_factor_formats_two_decimals():
    """``1.5`` and ``1.50`` must both render as ``1.50`` so floating-point
    representation jitter doesn't fragment the cache."""
    a = redis_keys.route_cache_key(
        engine_version="v", race_id="r", race_start=_RACE_START,
        first_snapshot_ref="a", last_snapshot_valid="b",
        snapshot_sources="x", safety_factor=1.5,
        venue=None, derating_tag="d", currents_tag="c",
    )
    b = redis_keys.route_cache_key(
        engine_version="v", race_id="r", race_start=_RACE_START,
        first_snapshot_ref="a", last_snapshot_valid="b",
        snapshot_sources="x", safety_factor=1.50,
        venue=None, derating_tag="d", currents_tag="c",
    )
    assert a == b
    assert ":1.50:" in a


def test_route_last_best_key_fingerprint():
    assert (
        redis_keys.route_last_best_key(_RACE_ID)
        == "route:last_best:11111111-2222-3333-4444-555555555555"
    )


def test_route_alternative_key_fingerprint():
    assert (
        redis_keys.route_alternative_key(_RACE_ID)
        == "route:alternative:11111111-2222-3333-4444-555555555555"
    )


def test_route_notifications_channel_fingerprint():
    assert (
        redis_keys.route_notifications_channel(_RACE_ID)
        == "route:notifications:11111111-2222-3333-4444-555555555555"
    )


def test_route_last_request_key_fingerprint():
    """New in Phase 2 — but the key shape is fixed here so the Phase 2
    writer and Phase 2 worker reader can't drift on it."""
    assert (
        redis_keys.route_last_request_key(_RACE_ID)
        == "route:last_request:11111111-2222-3333-4444-555555555555"
    )


def test_route_keys_accept_string_race_id():
    """UUID objects and string race_ids must produce identical keys —
    callers in some code paths have already stringified the UUID, others
    pass the raw object. Both flow through ``str()`` interpolation.
    """
    raw = UUID("11111111-2222-3333-4444-555555555555")
    assert (
        redis_keys.route_last_best_key(raw)
        == redis_keys.route_last_best_key(str(raw))
    )


# ---------------------------------------------------------------------------
# TTL constants — pin the values so a careless edit blows the test


def test_ttl_constants():
    assert redis_keys.ROUTE_CACHE_TTL_S == 3600
    assert redis_keys.ROUTE_NOTIFICATION_TTL_S == 7 * 24 * 3600
    assert redis_keys.ROUTE_LAST_REQUEST_TTL_S == 7 * 24 * 3600


# ---------------------------------------------------------------------------
# Tactician trace (Phase A observability, 2026-07-09)


def test_tactics_trace_key_fingerprint():
    assert (
        redis_keys.tactics_trace_key(_RACE_ID)
        == "tactics:trace:11111111-2222-3333-4444-555555555555"
    )


def test_tactics_trace_ttl():
    assert redis_keys.TACTICS_TRACE_TTL_S == 24 * 3600


# ---------------------------------------------------------------------------
# Dead-recorder watchdog


def test_recorder_watchdog_key_fingerprint():
    assert (
        redis_keys.recorder_watchdog_key(_RACE_ID)
        == "watchdog:recorder:11111111-2222-3333-4444-555555555555"
    )


def test_recorder_watchdog_ttl():
    assert redis_keys.RECORDER_WATCHDOG_TTL_S == 6 * 3600
