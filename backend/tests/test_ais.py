"""Tests for the AIS service module + read endpoint.

Coverage:
  * Subscribe-message construction (bbox transpose, message-type filter).
  * PositionReport parsing — happy path, sentinel rejection, missing-MMSI.
  * ShipStaticData identity merge — name/type from static lands on the
    next position from the same MMSI.
  * Round-trip via Redis fake — write then bbox-read.
  * read_positions_in_bbox respects limit + bbox filtering.
  * GET /api/ais HTTP contract.

External dependencies (the websockets package, real AISStream service)
are not exercised; ``run_subscription`` accepts a ``connect_factory``
override so its reconnect/cache loop can be driven by a fake socket.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import redis_client
from app.auth import get_current_user
from app.main import app
from app.services.ais import (
    AIS_KEY_PREFIX,
    AisPosition,
    build_subscribe_message,
    consume_messages,
    read_positions_in_bbox,
    run_subscription,
    write_position,
    _parse_position_report,
    _parse_ship_static,
)


# ─── In-memory Redis fake ───────────────────────────────────────────────


class _FakeRedis:
    """Minimal async Redis fake covering the AIS module's surface.

    Supports: setex, get, scan_iter. Sufficient for unit tests; production
    uses real redis-py async client.
    """
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def setex(self, key: str, ttl_s: int, value: str | bytes) -> None:
        # Ignore TTL for tests — they don't run long enough to expire.
        if isinstance(value, str):
            value = value.encode()
        self.store[key] = value

    async def get(self, key: str):
        return self.store.get(key)

    async def scan_iter(self, match: str, count: int = 200):
        # Trivial glob: "ais:vessel:*" pattern.
        if match.endswith("*"):
            prefix = match[:-1]
            for k in list(self.store):
                if k.startswith(prefix):
                    yield k
        else:
            if match in self.store:
                yield match


# ─── build_subscribe_message ────────────────────────────────────────────


def test_subscribe_message_transposes_bboxes():
    msg_str = build_subscribe_message(
        api_key="secret",
        bboxes=[(41.6, 42.5, -88.0, -87.2)],
    )
    msg = json.loads(msg_str)
    assert msg["APIKey"] == "secret"
    assert msg["BoundingBoxes"] == [[[41.6, -88.0], [42.5, -87.2]]]
    assert "PositionReport" in msg["FilterMessageTypes"]
    assert "ShipStaticData" in msg["FilterMessageTypes"]


def test_subscribe_message_handles_multiple_bboxes():
    msg = json.loads(build_subscribe_message(
        api_key="k",
        bboxes=[
            (41.6, 42.5, -88.0, -87.2),
            (42.7, 43.4, -88.1, -87.5),
        ],
    ))
    assert len(msg["BoundingBoxes"]) == 2


# ─── PositionReport parsing ─────────────────────────────────────────────


def _position_msg(**overrides):
    msg = {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 366742700, "ShipName": "EXAMPLE"},
        "Message": {"PositionReport": {
            "Latitude": 41.88, "Longitude": -87.62,
            "Sog": 5.4, "Cog": 180.0, "TrueHeading": 178,
        }},
    }
    msg["Message"]["PositionReport"].update(overrides)
    return msg


def test_parse_position_happy_path():
    pos = _parse_position_report(_position_msg())
    assert pos is not None
    assert pos.mmsi == 366742700
    assert pos.lat == pytest.approx(41.88)
    assert pos.lon == pytest.approx(-87.62)
    assert pos.sog_kts == pytest.approx(5.4)
    assert pos.cog_deg == pytest.approx(180.0)
    assert pos.heading_deg == pytest.approx(178)
    assert pos.name == "EXAMPLE"


def test_parse_position_cog_sentinel_rejected():
    pos = _parse_position_report(_position_msg(Cog=360.0))
    assert pos is not None
    assert pos.cog_deg is None


def test_parse_position_heading_sentinel_rejected():
    pos = _parse_position_report(_position_msg(TrueHeading=511))
    assert pos is not None
    assert pos.heading_deg is None


def test_parse_position_sog_sentinel_rejected():
    pos = _parse_position_report(_position_msg(Sog=102.3))
    assert pos is not None
    assert pos.sog_kts is None


def test_parse_position_missing_mmsi_returns_none():
    msg = _position_msg()
    msg["MetaData"] = {}
    msg["Message"]["PositionReport"].pop("UserID", None)
    assert _parse_position_report(msg) is None


def test_parse_position_invalid_coords_returns_none():
    assert _parse_position_report(_position_msg(Latitude=91.0)) is None
    assert _parse_position_report(_position_msg(Longitude=181.0)) is None


# ─── ShipStaticData parsing ─────────────────────────────────────────────


def test_parse_ship_static_extracts_name_and_type():
    msg = {
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": 366742700},
        "Message": {"ShipStaticData": {"Name": "VESSEL X", "Type": 70}},
    }
    result = _parse_ship_static(msg)
    assert result is not None
    mmsi, identity = result
    assert mmsi == 366742700
    assert identity["name"] == "VESSEL X"
    assert identity["ship_type"] == 70


# ─── consume_messages enrichment ────────────────────────────────────────


class _AsyncIterWS:
    """A fake WebSocket that yields a queued list of JSON messages."""
    def __init__(self, messages):
        self._messages = [
            m if isinstance(m, str) else json.dumps(m) for m in messages
        ]

    def __aiter__(self):
        async def _gen():
            for m in self._messages:
                yield m
        return _gen()


@pytest.mark.asyncio
async def test_consume_messages_merges_static_into_subsequent_positions():
    """Static record arrives first → subsequent positions get the name."""
    static_msg = {
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": 366742700},
        "Message": {"ShipStaticData": {"Name": "PRIMARY", "Type": 70}},
    }
    pos_msg = _position_msg()
    pos_msg["MetaData"]["ShipName"] = None  # static must fill the gap

    ws = _AsyncIterWS([static_msg, pos_msg])
    static_cache: dict = {}
    yielded = []
    async for pos in consume_messages(ws, static_cache=static_cache):
        yielded.append(pos)
    assert len(yielded) == 1
    assert yielded[0].name == "PRIMARY"
    assert yielded[0].ship_type == 70


@pytest.mark.asyncio
async def test_consume_messages_skips_unknown_types():
    """Non-Position/Static messages are silently dropped."""
    ws = _AsyncIterWS([{"MessageType": "BaseStationReport"}])
    yielded = [p async for p in consume_messages(ws, static_cache={})]
    assert yielded == []


@pytest.mark.asyncio
async def test_consume_messages_ignores_malformed_frames():
    ws = _AsyncIterWS(["not-json-at-all", _position_msg()])
    yielded = [p async for p in consume_messages(ws, static_cache={})]
    assert len(yielded) == 1


# ─── Redis round-trip ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_and_read_position_round_trip():
    redis = _FakeRedis()
    pos = AisPosition(
        mmsi=111, lat=42.0, lon=-87.5, sog_kts=5.0,
        last_seen_ts=time.time(),
    )
    await write_position(redis, pos)
    out = await read_positions_in_bbox(
        redis,
        min_lat=41.0, max_lat=43.0, min_lon=-88.0, max_lon=-87.0,
    )
    assert len(out) == 1
    assert out[0].mmsi == 111


@pytest.mark.asyncio
async def test_read_positions_filters_by_bbox():
    redis = _FakeRedis()
    in_box = AisPosition(mmsi=1, lat=42.0, lon=-87.5, last_seen_ts=time.time())
    out_of_box = AisPosition(mmsi=2, lat=30.0, lon=-87.5, last_seen_ts=time.time())
    await write_position(redis, in_box)
    await write_position(redis, out_of_box)
    out = await read_positions_in_bbox(
        redis,
        min_lat=41.0, max_lat=43.0, min_lon=-88.0, max_lon=-87.0,
    )
    assert [p.mmsi for p in out] == [1]


@pytest.mark.asyncio
async def test_read_positions_respects_limit():
    redis = _FakeRedis()
    for i in range(5):
        await write_position(redis, AisPosition(
            mmsi=i, lat=42.0, lon=-87.5, last_seen_ts=time.time(),
        ))
    out = await read_positions_in_bbox(
        redis,
        min_lat=41.0, max_lat=43.0, min_lon=-88.0, max_lon=-87.0,
        limit=3,
    )
    assert len(out) == 3


# ─── run_subscription with injected fake ────────────────────────────────


class _OneShotConnect:
    """connect_factory that yields a single fake socket once then raises.

    The second call raises asyncio.CancelledError so run_subscription
    exits its reconnect loop without infinite-looping in tests.
    """
    def __init__(self, messages):
        self.messages = messages
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        if self.calls > 1:
            raise asyncio.CancelledError("done")
        return self  # acts as the async context manager

    async def __aenter__(self):
        return _AsyncIterWSWithSend(self.messages)

    async def __aexit__(self, *exc):
        return False


class _AsyncIterWSWithSend(_AsyncIterWS):
    """Fake WS that also accepts a .send() call (the subscribe message)."""
    def __init__(self, messages):
        super().__init__(messages)
        self.sent: list[str] = []

    async def send(self, payload: str):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_run_subscription_writes_cached_positions():
    redis = _FakeRedis()
    fake_connect = _OneShotConnect([_position_msg()])
    with pytest.raises(asyncio.CancelledError):
        await run_subscription(
            api_key="k",
            bboxes=[(41.0, 43.0, -88.0, -87.0)],
            redis=redis,
            connect_factory=fake_connect,
            reconnect_backoff_s=0.0,
        )
    # One position should be in the cache.
    assert len(redis.store) == 1
    key = next(iter(redis.store))
    assert key.startswith(AIS_KEY_PREFIX)


# ─── HTTP endpoint ──────────────────────────────────────────────────────


@pytest.fixture
def fake_redis_with_one_vessel():
    redis = _FakeRedis()
    return redis


@pytest.fixture
def client(fake_redis_with_one_vessel):
    app.dependency_overrides[get_current_user] = lambda: {"uid": "test-uid"}
    with patch.object(
        redis_client, "get_client", return_value=fake_redis_with_one_vessel,
    ):
        yield TestClient(app)
    app.dependency_overrides.clear()


def test_ais_endpoint_returns_vessels_in_bbox(client, fake_redis_with_one_vessel):
    # Seed one vessel synchronously into the fake.
    asyncio.run(write_position(
        fake_redis_with_one_vessel,
        AisPosition(mmsi=999, lat=42.0, lon=-87.5,
                    sog_kts=4.2, name="Test", last_seen_ts=1234567890.0),
    ))
    r = client.get(
        "/api/ais",
        params={"min_lat": 41.0, "max_lat": 43.0,
                "min_lon": -88.0, "max_lon": -87.0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["vessels"][0]["mmsi"] == 999
    assert body["vessels"][0]["name"] == "Test"


def test_ais_endpoint_rejects_inverted_bbox(client):
    r = client.get(
        "/api/ais",
        params={"min_lat": 43.0, "max_lat": 41.0,
                "min_lon": -88.0, "max_lon": -87.0},
    )
    assert r.status_code == 400
