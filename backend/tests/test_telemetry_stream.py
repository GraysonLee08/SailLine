"""Tests for the WebSocket telemetry stream.

Strategy:
  - Mock auth (verify_ws_token) so we don't hit real Firebase.
  - Mock the two DB helpers (_race_belongs_to_user, _load_calibration)
    so we don't hit real Postgres.
  - Do NOT mock the Kalman filter — feed real samples through and assert
    on real output. The filter itself has its own unit tests in
    test_attitude.py; here we verify the integration path.
  - Override the db.get_pool dependency with a sentinel so the WS
    handler's Depends resolution succeeds without a live pool.

Patches are applied to `app.routers.telemetry_stream` (the module's
local names), not to `app.auth` or `app.db`, because the router has
already bound those names at import time.

Performance note: the filter is deterministic — N+1 IMU samples produce
exactly N attitude messages. Tests read exactly that many; reading more
would block on the next heartbeat (15 s default), turning a sub-second
test into a 75 s test.
"""

from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import db
from app.auth import InvalidTokenError
from app.main import app
from app.routers import telemetry_stream


TEST_RACE_ID = "11111111-2222-3333-4444-555555555555"
G = 9.81  # m/s^2


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def override_pool():
    """Replace db.get_pool with a sentinel so Depends resolution succeeds.

    The handler's helpers are all monkey-patched, so the pool object is
    never actually used — any non-None sentinel works.
    """
    sentinel = object()
    app.dependency_overrides[db.get_pool] = lambda: sentinel
    yield
    app.dependency_overrides.pop(db.get_pool, None)


@pytest.fixture
def auth_ok(monkeypatch):
    """Token 'valid' succeeds as test-user; anything else raises."""
    async def fake_verify(token, pool):
        if token == "valid":
            return {
                "uid": "test-user",
                "email": "t@test.com",
                "tier": "free",
                "claims": {},
            }
        raise InvalidTokenError("test")

    monkeypatch.setattr(telemetry_stream, "verify_ws_token", fake_verify)


@pytest.fixture
def race_owned(monkeypatch):
    """Race is owned by test-user."""
    async def fake_check(pool, race_id, user_id):
        return user_id == "test-user"

    monkeypatch.setattr(telemetry_stream, "_race_belongs_to_user", fake_check)


@pytest.fixture
def cal_none(monkeypatch):
    """No calibration recorded."""
    async def fake_load(pool, race_id):
        return 0.0, 0.0

    monkeypatch.setattr(telemetry_stream, "_load_calibration", fake_load)


@pytest.fixture
def cal_offsets(monkeypatch):
    """Calibration: heel +5°, pitch -2°."""
    async def fake_load(pool, race_id):
        return 5.0, -2.0

    monkeypatch.setattr(telemetry_stream, "_load_calibration", fake_load)


@pytest.fixture
def client():
    return TestClient(app)


# ─── Helpers ─────────────────────────────────────────────────────────


def _imu(t, ax, ay, az, gx=0.0, gy=0.0, gz=0.0) -> str:
    """Build an IMU message as a JSON string."""
    return json.dumps({
        "t": t, "ax": ax, "ay": ay, "az": az,
        "gx": gx, "gy": gy, "gz": gz,
    })


def _read_attitudes(ws, expected: int) -> list[dict]:
    """Read exactly `expected` attitude messages and return them.

    Reads only as many WS frames as we expect to be produced. Reading
    more would block on heartbeats (15 s default) and turn fast tests
    into slow ones. The filter is deterministic — N+1 IMU samples
    produce exactly N attitude messages — so callers know `expected`
    precisely.
    """
    attitudes: list[dict] = []
    # Heartbeats can interleave under squashed-interval test conditions,
    # so loop with a generous cap rather than reading exactly `expected`
    # frames. The cap is small enough never to wait on a real heartbeat
    # at default interval.
    for _ in range(expected + 2):
        msg = json.loads(ws.receive_text())
        if msg["type"] == "attitude":
            attitudes.append(msg)
            if len(attitudes) == expected:
                return attitudes
    pytest.fail(f"expected {expected} attitude messages, got {len(attitudes)}")


# ─── Handshake auth ──────────────────────────────────────────────────


class TestHandshakeAuth:
    """All validation happens before .accept() — failures look like
    handshake rejections, not accept-then-close."""

    def test_missing_token_rejects(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/api/races/{TEST_RACE_ID}/telemetry/stream"
            ):
                pass

    def test_invalid_token_rejects(self, client, auth_ok):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=bogus"
            ):
                pass

    def test_valid_token_unowned_race_rejects(self, client, auth_ok, monkeypatch):
        async def deny(pool, race_id, user_id):
            return False

        async def cal(pool, race_id):
            return 0.0, 0.0

        monkeypatch.setattr(telemetry_stream, "_race_belongs_to_user", deny)
        monkeypatch.setattr(telemetry_stream, "_load_calibration", cal)

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=valid"
            ):
                pass

    def test_valid_handshake_accepts(self, client, auth_ok, race_owned, cal_none):
        with client.websocket_connect(
            f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=valid"
        ) as ws:
            # First sample initializes (no output); second produces 1 attitude.
            ws.send_text(_imu(0.0, 0, 0, G))
            ws.send_text(_imu(0.1, 0, 0, G))
            [attitude] = _read_attitudes(ws, expected=1)
            assert attitude["type"] == "attitude"
            assert "heel_deg" in attitude
            assert "pitch_deg" in attitude
            assert "t" in attitude


# ─── Filter integration ──────────────────────────────────────────────


class TestFilterIntegration:
    """Real filter, real math — confirm the end-to-end path is sane."""

    def test_phone_flat_yields_near_zero_attitude(
        self, client, auth_ok, race_owned, cal_none,
    ):
        with client.websocket_connect(
            f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=valid"
        ) as ws:
            ws.send_text(_imu(0.0, 0, 0, G))  # init
            for i in range(1, 11):
                ws.send_text(_imu(i * 0.1, 0, 0, G))

            attitudes = _read_attitudes(ws, expected=10)
            last = attitudes[-1]
            assert abs(last["heel_deg"]) < 1.0
            assert abs(last["pitch_deg"]) < 1.0

    def test_constant_heel_recovered(
        self, client, auth_ok, race_owned, cal_none,
    ):
        """Phone held at 20° starboard heel → filter should track."""
        heel_rad = math.radians(20.0)
        ax = G * math.sin(heel_rad)
        az = G * math.cos(heel_rad)

        with client.websocket_connect(
            f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=valid"
        ) as ws:
            ws.send_text(_imu(0.0, ax, 0, az))  # init
            for i in range(1, 21):
                ws.send_text(_imu(i * 0.1, ax, 0, az))

            attitudes = _read_attitudes(ws, expected=20)
            last = attitudes[-1]
            assert abs(last["heel_deg"] - 20.0) < 1.0


# ─── Calibration application ─────────────────────────────────────────


class TestCalibration:
    """Per-race offsets must be subtracted from filter output."""

    def test_offsets_applied_to_outgoing_attitude(
        self, client, auth_ok, race_owned, cal_offsets,
    ):
        """With heel_offset=+5°, pitch_offset=-2°, a flat phone reports
        heel=-5, pitch=+2 (raw filter ~= 0,0; subtract offsets)."""
        with client.websocket_connect(
            f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=valid"
        ) as ws:
            ws.send_text(_imu(0.0, 0, 0, G))  # init
            for i in range(1, 11):
                ws.send_text(_imu(i * 0.1, 0, 0, G))

            attitudes = _read_attitudes(ws, expected=10)
            last = attitudes[-1]
            assert abs(last["heel_deg"] - (-5.0)) < 1.0
            assert abs(last["pitch_deg"] - 2.0) < 1.0


# ─── Schema validation ───────────────────────────────────────────────


class TestSchemaValidation:
    """Bad messages should be dropped, connection stays alive."""

    def test_malformed_json_does_not_close(
        self, client, auth_ok, race_owned, cal_none,
    ):
        with client.websocket_connect(
            f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=valid"
        ) as ws:
            ws.send_text("not json at all")
            ws.send_text("{partial: 'json")
            # Connection survives — good messages still produce attitude
            ws.send_text(_imu(0.0, 0, 0, G))
            ws.send_text(_imu(0.1, 0, 0, G))
            [attitude] = _read_attitudes(ws, expected=1)
            assert attitude["type"] == "attitude"

    def test_missing_fields_dropped(
        self, client, auth_ok, race_owned, cal_none,
    ):
        with client.websocket_connect(
            f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=valid"
        ) as ws:
            ws.send_text(json.dumps({"t": 0.0, "ax": 0}))  # missing rest
            ws.send_text(_imu(0.0, 0, 0, G))
            ws.send_text(_imu(0.1, 0, 0, G))
            [attitude] = _read_attitudes(ws, expected=1)
            assert attitude["type"] == "attitude"

    def test_out_of_range_dropped(
        self, client, auth_ok, race_owned, cal_none,
    ):
        with client.websocket_connect(
            f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=valid"
        ) as ws:
            ws.send_text(_imu(0.0, 1000.0, 0, G))  # ax out of range
            ws.send_text(_imu(0.0, 0, 0, G))
            ws.send_text(_imu(0.1, 0, 0, G))
            [attitude] = _read_attitudes(ws, expected=1)
            assert attitude["type"] == "attitude"


# ─── Heartbeat ───────────────────────────────────────────────────────


class TestHeartbeat:
    """Heartbeats fire periodically to keep Cloud Run from idle-closing."""

    def test_heartbeat_fires(
        self, client, auth_ok, race_owned, cal_none, monkeypatch,
    ):
        # Squash the interval to something we can wait for in a unit test.
        monkeypatch.setattr(telemetry_stream, "HEARTBEAT_INTERVAL_S", 0.05)

        with client.websocket_connect(
            f"/api/races/{TEST_RACE_ID}/telemetry/stream?token=valid"
        ) as ws:
            # Send nothing — wait for a heartbeat.
            for _ in range(10):
                msg = json.loads(ws.receive_text())
                if msg["type"] == "heartbeat":
                    assert "t" in msg
                    return
            pytest.fail("no heartbeat received within 10 messages")