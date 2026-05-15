"""Tests for app/auth_helpers.py.

These are SQL fragment generators — pure functions over strings. We
assert the shape (must contain the right table aliases and parameter
placeholders) and the role filtering.

Behavioural correctness against a real Postgres is exercised by the
router-level tests in test_races_router, test_tracks_router,
test_race_stats_router, test_boats_router, and test_crew_router.
"""
from __future__ import annotations

from app.auth_helpers import (
    boat_owner_predicate,
    boat_read_predicate,
    race_owner_predicate,
    race_read_predicate,
    race_write_predicate,
)


# ─── Race predicates ─────────────────────────────────────────────────


def test_race_read_predicate_includes_user_id_and_membership():
    pred = race_read_predicate(race_alias="r", uid_placeholder="$2")
    assert "r.user_id = $2" in pred
    assert "boat_crew" in pred
    assert "$2" in pred
    # Read predicate has no role filter — viewers can read.
    assert "role" not in pred


def test_race_write_predicate_filters_to_owner_and_crew():
    pred = race_write_predicate(race_alias="r", uid_placeholder="$2")
    assert "r.user_id = $2" in pred
    assert "'owner'" in pred
    assert "'crew'" in pred
    assert "'viewer'" not in pred


def test_race_owner_predicate_is_strictest():
    pred = race_owner_predicate(race_alias="r", uid_placeholder="$2")
    assert "'owner'" in pred
    assert "'crew'" not in pred


def test_race_predicate_respects_alias():
    pred = race_read_predicate(race_alias="rs", uid_placeholder="$1")
    assert "rs.user_id" in pred
    assert "rs.boat_id" in pred
    assert "r." not in pred.replace("rs.", "")   # only the rs.X form


# ─── Boat predicates ─────────────────────────────────────────────────


def test_boat_read_predicate_uses_owner_id_and_crew():
    pred = boat_read_predicate(boat_alias="b", uid_placeholder="$2")
    assert "b.owner_id = $2" in pred
    assert "bc.boat_id = b.id" in pred


def test_boat_owner_predicate_restricts_to_owner_role():
    pred = boat_owner_predicate(boat_alias="b", uid_placeholder="$2")
    assert "'owner'" in pred
    assert "'crew'" not in pred
    assert "'viewer'" not in pred
