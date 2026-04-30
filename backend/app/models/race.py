"""Pydantic models for race setup.

The `Course` model defines the JSONB shape stored in race_sessions.course.
Both inshore and distance modes use the same shape; mode is metadata on
the parent RaceSession that affects routing-engine cadence and UI presentation.

Source of truth for:
- The boat-class enum (CHECK constraint deliberately omitted from SQL so
  adding a class is a Python deploy, not a migration)
- The course JSON shape (validated end-to-end before it ever hits the DB)
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums

class BoatClass(str, Enum):
    """Boat classes shipped at v1 launch.

    Add a class by appending here and redeploying — no SQL change needed.
    """
    BENETEAU_36_7 = "Beneteau First 36.7"
    J_105 = "J/105"
    J_109 = "J/109"
    J_111 = "J/111"
    FARR_40 = "Farr 40"
    BENETEAU_40_7 = "Beneteau First 40.7"
    TARTAN_10 = "Tartan 10"
    GENERIC_PHRF_ORC = "Generic PHRF/ORC"


class RaceMode(str, Enum):
    INSHORE = "inshore"
    DISTANCE = "distance"


class Rounding(str, Enum):
    PORT = "port"
    STARBOARD = "starboard"


# ---------------------------------------------------------------------------
# Course shape (stored as JSONB in race_sessions.course)


class Mark(BaseModel):
    """A single point on the racecourse — buoy, start pin, finish, etc.

    Start/finish lines are modeled as single points (line midpoint) for v1.
    Real two-point lines can be added later via an optional line_to field
    without breaking existing rows.
    """
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class CourseStep(BaseModel):
    """One mark in the lap sequence. `rounding` is omitted for start/finish."""
    model_config = ConfigDict(extra="forbid")

    mark_id: str = Field(min_length=1, max_length=64)
    rounding: Rounding | None = None


class Course(BaseModel):
    """The course JSON stored in race_sessions.course.

    `course` describes one lap. `laps` multiplies it for inshore racing;
    distance races are `laps: 1`. Users can also store a fully expanded
    sequence with `laps: 1` if they prefer — both forms are valid.
    """
    model_config = ConfigDict(extra="forbid")

    marks: list[Mark] = Field(min_length=2)
    course: list[CourseStep] = Field(min_length=2)
    laps: int = Field(ge=1, default=1)

    @field_validator("marks")
    @classmethod
    def _mark_ids_unique(cls, v: list[Mark]) -> list[Mark]:
        ids = [m.id for m in v]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate mark ids: {dupes}")
        return v

    @model_validator(mode="after")
    def _course_steps_reference_known_marks(self) -> "Course":
        known = {m.id for m in self.marks}
        for i, step in enumerate(self.course):
            if step.mark_id not in known:
                raise ValueError(
                    f"course[{i}].mark_id={step.mark_id!r} is not in marks; "
                    f"known: {sorted(known)}"
                )
        return self


# ---------------------------------------------------------------------------
# Request/response shapes for the API endpoints (built next session)


class RaceSessionCreate(BaseModel):
    """POST /api/races request body."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    mode: RaceMode
    boat_class: BoatClass
    course: Course
