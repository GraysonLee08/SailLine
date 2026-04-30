"""Tests for app/models/race.py.

Locks down the course JSON contract that the future POST /api/races endpoint
and the frontend will both render against. Pure validation tests — no DB,
no network. Run with:
    python -m pytest tests/test_race_models.py -v
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.race import (
    BoatClass,
    Course,
    CourseStep,
    Mark,
    RaceMode,
    RaceSessionCreate,
    Rounding,
)


# ---------------------------------------------------------------------------
# Fixtures — realistic example courses


def _inshore_payload() -> dict:
    """3-lap windward-leeward off Chicago."""
    return {
        "name": "Saturday MORF Race 14",
        "mode": "inshore",
        "boat_class": "J/105",
        "course": {
            "marks": [
                {"id": "S", "name": "Start/Finish", "lat": 41.920, "lon": -87.610},
                {"id": "W", "name": "Windward",     "lat": 41.955, "lon": -87.605},
                {"id": "L", "name": "Leeward",      "lat": 41.890, "lon": -87.615},
            ],
            "course": [
                {"mark_id": "S"},
                {"mark_id": "W", "rounding": "port"},
                {"mark_id": "L", "rounding": "port"},
                {"mark_id": "S"},
            ],
            "laps": 3,
        },
    }


def _distance_payload() -> dict:
    """Chicago-Mac, simplified to one waypoint + finish."""
    return {
        "name": "Race to Mackinac 2026",
        "mode": "distance",
        "boat_class": "Beneteau First 36.7",
        "course": {
            "marks": [
                {"id": "start",  "name": "Chicago Start",   "lat": 41.892, "lon": -87.601},
                {"id": "gray",   "name": "Gray's Reef",     "lat": 45.770, "lon": -85.130},
                {"id": "finish", "name": "Mackinac Finish", "lat": 45.847, "lon": -84.620},
            ],
            "course": [
                {"mark_id": "start"},
                {"mark_id": "gray", "rounding": "starboard"},
                {"mark_id": "finish"},
            ],
            "laps": 1,
        },
    }


# ---------------------------------------------------------------------------
# Happy paths


def test_valid_inshore_payload_parses():
    race = RaceSessionCreate(**_inshore_payload())
    assert race.mode is RaceMode.INSHORE
    assert race.boat_class is BoatClass.J_105
    assert race.course.laps == 3
    assert len(race.course.marks) == 3
    assert race.course.course[1].rounding is Rounding.PORT


def test_valid_distance_payload_parses():
    race = RaceSessionCreate(**_distance_payload())
    assert race.mode is RaceMode.DISTANCE
    assert race.course.laps == 1
    # Start and finish have no rounding; only Gray's Reef does
    assert race.course.course[0].rounding is None
    assert race.course.course[1].rounding is Rounding.STARBOARD
    assert race.course.course[2].rounding is None


def test_default_laps_is_one():
    """laps is optional in the JSON; defaults to 1."""
    payload = _distance_payload()
    del payload["course"]["laps"]
    race = RaceSessionCreate(**payload)
    assert race.course.laps == 1


def test_round_trip_through_json():
    """Serialize and reload — the JSON shape is stable for JSONB storage."""
    original = RaceSessionCreate(**_inshore_payload())
    json_str = original.model_dump_json()
    reloaded = RaceSessionCreate.model_validate_json(json_str)
    assert reloaded == original


# ---------------------------------------------------------------------------
# Validation rules


def test_rejects_course_step_with_unknown_mark_id():
    payload = _inshore_payload()
    payload["course"]["course"][1]["mark_id"] = "ZZZ"
    with pytest.raises(ValidationError, match="not in marks"):
        RaceSessionCreate(**payload)


def test_rejects_duplicate_mark_ids():
    payload = _inshore_payload()
    payload["course"]["marks"][1]["id"] = "S"  # collide with the start mark
    with pytest.raises(ValidationError, match="duplicate mark ids"):
        RaceSessionCreate(**payload)


@pytest.mark.parametrize(
    "lat, lon",
    [
        (91.0, 0.0),       # lat too high
        (-91.0, 0.0),      # lat too low
        (0.0, 181.0),      # lon too high
        (0.0, -181.0),     # lon too low
    ],
)
def test_rejects_out_of_range_coordinates(lat: float, lon: float):
    payload = _inshore_payload()
    payload["course"]["marks"][0]["lat"] = lat
    payload["course"]["marks"][0]["lon"] = lon
    with pytest.raises(ValidationError):
        RaceSessionCreate(**payload)


@pytest.mark.parametrize("laps", [0, -1, -100])
def test_rejects_laps_below_one(laps: int):
    payload = _inshore_payload()
    payload["course"]["laps"] = laps
    with pytest.raises(ValidationError):
        RaceSessionCreate(**payload)


def test_rejects_under_two_marks():
    payload = _inshore_payload()
    payload["course"]["marks"] = payload["course"]["marks"][:1]
    with pytest.raises(ValidationError):
        RaceSessionCreate(**payload)


def test_rejects_under_two_course_steps():
    payload = _inshore_payload()
    payload["course"]["course"] = payload["course"]["course"][:1]
    with pytest.raises(ValidationError):
        RaceSessionCreate(**payload)


def test_rejects_unknown_boat_class():
    payload = _inshore_payload()
    payload["boat_class"] = "Optimist"  # not at v1 launch
    with pytest.raises(ValidationError):
        RaceSessionCreate(**payload)


def test_rejects_unknown_mode():
    payload = _inshore_payload()
    payload["mode"] = "offshore"
    with pytest.raises(ValidationError):
        RaceSessionCreate(**payload)


def test_rejects_unknown_rounding():
    payload = _inshore_payload()
    payload["course"]["course"][1]["rounding"] = "leftward"
    with pytest.raises(ValidationError):
        RaceSessionCreate(**payload)


def test_rejects_extra_fields():
    """Strict schema: typos like 'lng' instead of 'lon' fail loudly."""
    payload = _inshore_payload()
    payload["course"]["marks"][0]["lng"] = -87.610
    with pytest.raises(ValidationError):
        RaceSessionCreate(**payload)


def test_rejects_empty_name():
    payload = _inshore_payload()
    payload["name"] = ""
    with pytest.raises(ValidationError):
        RaceSessionCreate(**payload)
