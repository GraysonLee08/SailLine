# backend/tests/test_telemetry_wire_contract.py
"""Cross-language wire-format contract test.

Validates the canonical GPS wire sample that the shared JS serializer
(``packages/shared/src/telemetry.js::gpsPointToWire``) emits against the
backend Pydantic models. The same fixture is asserted on the JS side by
``packages/shared/src/telemetry.test.js``, so this pair pins the
client -> server telemetry contract on both sides of the language
boundary: if either the JS serializer or the Pydantic schema drifts, one
of the two tests fails.

This is a pure model-validation test — no DB, no auth, no FastAPI client.
It complements ``test_telemetry.py`` (which covers endpoint behaviour);
it does not duplicate it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.routers.telemetry import GpsSample, TelemetryBatch

# Single source of truth, shared with the JS test. Path is resolved from
# this file (not cwd) so it works regardless of where pytest is invoked.
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "shared"
    / "src"
    / "__fixtures__"
    / "telemetry_wire_sample.json"
)


@pytest.fixture(scope="module")
def wire_sample() -> dict:
    if not FIXTURE.exists():
        pytest.skip(
            "shared wire fixture not present (backend-only checkout): "
            f"{FIXTURE}"
        )
    return json.loads(FIXTURE.read_text())


def test_fixture_validates_against_telemetry_batch(wire_sample: dict):
    """The whole batch parses cleanly into TelemetryBatch."""
    batch = TelemetryBatch.model_validate(wire_sample)
    assert len(batch.gps) == 3
    # imu/calibration absent in the sample -> defaults.
    assert batch.imu == []
    assert batch.calibration is None


def test_full_fix_round_trips(wire_sample: dict):
    """A fully-populated fix keeps every field through validation."""
    first = GpsSample.model_validate(wire_sample["gps"][0])
    assert first.lat == 41.935
    assert first.lon == -87.677
    assert first.sog_kts == 5.4
    assert first.cog_deg == 210.5
    assert first.gps_acc_m == 4.0


def test_null_velocity_fields_accepted(wire_sample: dict):
    """A first-fix sample with null sog/cog is valid (optional fields)."""
    sample = GpsSample.model_validate(wire_sample["gps"][1])
    assert sample.sog_kts is None
    assert sample.cog_deg is None
    assert sample.gps_acc_m == 6.5


def test_zero_speed_and_null_course_accepted(wire_sample: dict):
    """Zero SOG is a real value (kept, not nulled); course is null."""
    sample = GpsSample.model_validate(wire_sample["gps"][2])
    assert sample.sog_kts == 0
    assert sample.cog_deg is None
