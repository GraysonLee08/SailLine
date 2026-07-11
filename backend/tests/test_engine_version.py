# backend/tests/test_engine_version.py
"""Pin :data:`ENGINE_VERSION` to the :class:`RouteRequest` /
:class:`DeratingProfile` field set it was bumped for.

ENGINE_VERSION is part of the route cache key. Adding a knob to either
dataclass without bumping the version means cached entries written
under the old shape (and computed without the new knob) are still
hit — silently wrong.

This test snapshots the field set + version. If you add a knob:

  1. Add it to :class:`RouteRequest` or :class:`DeratingProfile` with a
     default that preserves current behaviour.
  2. Bump :data:`ENGINE_VERSION` in
     ``app/services/routing/pipeline.py``.
  3. Update the snapshot below to the new ``(field_set, version)``
     tuple.

The test then re-greens. Skipping any of these three steps is a bug.
"""
from __future__ import annotations

from dataclasses import fields

from app.services.routing.pipeline import (
    ENGINE_VERSION,
    DeratingProfile,
    RouteRequest,
)


# Pinned snapshot. Update IN THE SAME COMMIT as the ENGINE_VERSION bump.
# v13-maneuver: tack/gybe penalties, top-2 frontier per bearing bin,
# maneuver tie-break in culling. Engine behaviour change only — no
# RouteRequest/DeratingProfile field changes.
_PINNED_VERSION = "v13-maneuver"

_PINNED_ROUTE_REQUEST_FIELDS = frozenset({
    "race_id",
    "marks",
    "race_start",
    "boat_class",
    "safety_factor",
    "duration_hours",
    "derating",
})

_PINNED_DERATING_FIELDS = frozenset({
    "max_tws_kt",
    "polar_margin",
    "hs_m",
    "density_factor",
})


def test_engine_version_matches_snapshot():
    assert ENGINE_VERSION == _PINNED_VERSION, (
        f"ENGINE_VERSION changed to {ENGINE_VERSION!r} without updating "
        f"this test. If you added/removed a knob, update the field sets "
        f"and bump _PINNED_VERSION here."
    )


def test_route_request_field_set_matches_snapshot():
    current = {f.name for f in fields(RouteRequest)}
    assert current == _PINNED_ROUTE_REQUEST_FIELDS, (
        f"RouteRequest fields changed (added: {current - _PINNED_ROUTE_REQUEST_FIELDS}, "
        f"removed: {_PINNED_ROUTE_REQUEST_FIELDS - current}). "
        f"Bump ENGINE_VERSION in pipeline.py and update _PINNED_ROUTE_REQUEST_FIELDS here."
    )


def test_derating_profile_field_set_matches_snapshot():
    current = {f.name for f in fields(DeratingProfile)}
    assert current == _PINNED_DERATING_FIELDS, (
        f"DeratingProfile fields changed (added: {current - _PINNED_DERATING_FIELDS}, "
        f"removed: {_PINNED_DERATING_FIELDS - current}). "
        f"Bump ENGINE_VERSION in pipeline.py and update _PINNED_DERATING_FIELDS here."
    )
