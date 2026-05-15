"""Centralised SQL auth predicates used by race-scoped routers.

Sharing in D3 means a race or boat can be accessed by:
  * its creator (race.user_id / boat.owner_id) — backwards-compatible
  * any user with a boat_crew row for the race's boat / the boat itself

These predicates are SQL fragments (not full queries) so routers can
splice them into their existing SELECT / UPDATE statements at the
right parameter offsets. The fragments use ``{uid}`` and ``{bid}`` /
``{rid}`` as template placeholders that the caller substitutes with
the actual asyncpg parameter numbers ($1, $2, ...).

Why string-templated and not asyncpg-bound directly: each router
builds its query at a different parameter offset (race id at $1,
uid at $2, etc.). Templating the predicate keeps the substitution
explicit at the call site without adding a query builder.

Roles:
  * 'owner'  — created the resource; full write access
  * 'crew'   — read + write (record tracks, edit race plans)
  * 'viewer' — read-only

Helpers below pre-build the fragments for the common cases. When a
router needs a one-off shape (e.g. owner-only writes), it builds the
predicate inline; the patterns are consistent enough that this is
readable.

Tested via the persona-style auth tests in
backend/tests/test_auth_predicates.py.
"""
from __future__ import annotations


def race_read_predicate(*, race_alias: str, uid_placeholder: str) -> str:
    """Predicate for "this caller can READ the race at <race_alias>".

    Matches when the caller created the race OR is a member of the
    boat the race belongs to at ANY role. NULL boat_id (legacy or
    unattached race) falls through to the creator-only check.
    """
    r = race_alias
    return (
        f"({r}.user_id = {uid_placeholder} OR EXISTS ("
        f"SELECT 1 FROM boat_crew bc "
        f"WHERE bc.boat_id = {r}.boat_id "
        f"AND bc.user_id = {uid_placeholder}))"
    )


def race_write_predicate(*, race_alias: str, uid_placeholder: str) -> str:
    """Predicate for "this caller can WRITE the race at <race_alias>".

    Matches when the caller created the race OR is a member of the
    boat the race belongs to with role IN ('owner', 'crew')."""
    r = race_alias
    return (
        f"({r}.user_id = {uid_placeholder} OR EXISTS ("
        f"SELECT 1 FROM boat_crew bc "
        f"WHERE bc.boat_id = {r}.boat_id "
        f"AND bc.user_id = {uid_placeholder} "
        f"AND bc.role IN ('owner', 'crew')))"
    )


def race_owner_predicate(*, race_alias: str, uid_placeholder: str) -> str:
    """Predicate for "this caller is the OWNER of the race".

    Owner = race creator, OR member of the race's boat with
    role='owner'. Used for owner-only ops (delete race, regenerate
    AI summary, etc.)."""
    r = race_alias
    return (
        f"({r}.user_id = {uid_placeholder} OR EXISTS ("
        f"SELECT 1 FROM boat_crew bc "
        f"WHERE bc.boat_id = {r}.boat_id "
        f"AND bc.user_id = {uid_placeholder} "
        f"AND bc.role = 'owner'))"
    )


def boat_read_predicate(*, boat_alias: str, uid_placeholder: str) -> str:
    """Predicate for "this caller can READ the boat".

    Matches when the caller is the boat's owner OR a member at any
    role. The ``boats.owner_id`` column is still authoritative for
    "creator"; ``boat_crew`` is the membership table."""
    b = boat_alias
    return (
        f"({b}.owner_id = {uid_placeholder} OR EXISTS ("
        f"SELECT 1 FROM boat_crew bc "
        f"WHERE bc.boat_id = {b}.id "
        f"AND bc.user_id = {uid_placeholder}))"
    )


def boat_owner_predicate(*, boat_alias: str, uid_placeholder: str) -> str:
    """Predicate for "this caller OWNS the boat".

    Used for boat edit / delete / cert upload."""
    b = boat_alias
    return (
        f"({b}.owner_id = {uid_placeholder} OR EXISTS ("
        f"SELECT 1 FROM boat_crew bc "
        f"WHERE bc.boat_id = {b}.id "
        f"AND bc.user_id = {uid_placeholder} "
        f"AND bc.role = 'owner'))"
    )


# Convenience for direct-by-id checks. Returns a fragment usable in
# WHERE clauses where the boat / race id is also a parameter:
#
#   WHERE r.id = $1 AND <race_read_predicate('r', '$2')>
#
# The functions above produce the inner parens; callers compose.


async def user_role_for_boat(
    conn, boat_id, uid: str,
) -> str | None:
    """Return the caller's role on a boat, or None if not a member.

    Helper for endpoints that need to make role-conditional choices
    *inside* Python (e.g. "render edit controls only for owner+crew").
    Most auth checks happen at the SQL predicate level above; this is
    for the few places that need the role explicitly.
    """
    row = await conn.fetchrow(
        "SELECT role FROM boat_crew WHERE boat_id = $1 AND user_id = $2",
        boat_id, uid,
    )
    return row["role"] if row else None
