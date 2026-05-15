"""Crew + invite endpoints — Session D3.

Two URL prefixes share this file:
  * ``/api/boats/{boat_id}/crew/*``    — manage the boat's members
  * ``/api/boats/{boat_id}/invites/*`` — create/list/revoke invites
  * ``/api/invites/redeem``            — recipient redeems an invite

Roles enforced at the SQL level via predicates in
``app/auth_helpers.py``. Owner-only ops use the strict owner check;
read endpoints use the membership check.

Invites are stored in a single ``boat_invites`` table with two
flavours discriminated by ``single_use``:
  * email invite → single_use=TRUE, email set, long UUID code, 7-day expiry
  * join code    → single_use=FALSE, email NULL, short readable code, no expiry

The redeem endpoint takes a code and:
  * 404 if not found
  * 410 if expired
  * 409 if single-use and already redeemed
  * 200 otherwise; creates the boat_crew row and (for single-use)
    marks the invite redeemed

D4 update: ``list_crew`` now JOINs ``user_profiles`` and surfaces
``email``, ``display_name``, and ``avatar_url`` — that's what makes
crew rows render as "Grayson V." instead of a raw Firebase UID.
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app import db
from app.auth import get_current_user
from app.auth_helpers import boat_owner_predicate, boat_read_predicate
from app.config import get_settings
from app.services.email import send_boat_invite

log = logging.getLogger(__name__)

router = APIRouter(tags=["crew"])


# Default email-invite expiry. Long enough that crew can accept on
# their next sail check-in; short enough to bound the blast radius of
# a forwarded email.
_DEFAULT_INVITE_EXPIRY_DAYS = 7

# Join-code format: human-friendly. 6 chars after a "RACE-" prefix
# from A-Z + 2-9 (no 0/1/I/O — visually confusing). 4 chars from a
# 32-char alphabet = ~1M possibilities; uniqueness is enforced by the
# table's UNIQUE constraint with retry on collision.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 4
_CODE_PREFIX = "RACE-"


# ─── Models ──────────────────────────────────────────────────────────


class CrewMemberOut(BaseModel):
    """One row in the crew list.

    D4: ``display_name``, ``email``, and ``avatar_url`` are pulled
    from ``user_profiles`` via a LEFT JOIN. They may be ``None`` for
    members whose profile pre-dates the D4 migration and who haven't
    visited the app since (the auth UPSERT backfills email + name on
    first contact).
    """

    user_id: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    role: Literal["owner", "crew", "viewer"]
    joined_at: str


class CrewRoleUpdate(BaseModel):
    role: Literal["crew", "viewer"]   # owners can't be patched to/from here


class InviteCreate(BaseModel):
    role: Literal["crew", "viewer"]
    # When set → email invite, single-use, 7-day expiry, SendGrid send.
    # When None → join code, multi-use, owner shares manually.
    email: Optional[EmailStr] = None
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=90)


class InviteOut(BaseModel):
    id: UUID
    boat_id: UUID
    role: str
    code: str
    email: Optional[str] = None
    single_use: bool
    expires_at: Optional[str] = None
    redeemed_at: Optional[str] = None
    created_at: str
    accept_url: str
    emailed: bool = False


class InviteRedeem(BaseModel):
    code: str = Field(min_length=4, max_length=128)


class InviteRedeemed(BaseModel):
    boat_id: UUID
    role: str


# ─── Helpers ─────────────────────────────────────────────────────────


async def _require_boat_owner(
    conn: asyncpg.Connection, boat_id: UUID, uid: str,
) -> None:
    """404 unless caller owns the boat (boats.owner_id OR boat_crew
    role='owner'). Used to gate every write in this router."""
    pred = boat_owner_predicate(boat_alias="b", uid_placeholder="$2")
    row = await conn.fetchrow(
        f"SELECT 1 FROM boats b WHERE b.id = $1 AND {pred}",
        boat_id, uid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "boat not found")


async def _require_boat_member(
    conn: asyncpg.Connection, boat_id: UUID, uid: str,
) -> None:
    """404 unless caller can READ the boat (any role + creator)."""
    pred = boat_read_predicate(boat_alias="b", uid_placeholder="$2")
    row = await conn.fetchrow(
        f"SELECT 1 FROM boats b WHERE b.id = $1 AND {pred}",
        boat_id, uid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "boat not found")


def _generate_join_code() -> str:
    """Short readable code with a prefix. Caller retries on UNIQUE
    collision (rare)."""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    return f"{_CODE_PREFIX}{body}"


def _generate_email_token() -> str:
    """Long opaque single-use token for email invites — UUID hex.
    Always unique in practice; we still let the DB enforce it."""
    return uuid4().hex


def _build_accept_url(code: str) -> str:
    base = get_settings().app_base_url.rstrip("/")
    return f"{base}/?invite={code}"


def _format_invite_row(row: asyncpg.Record) -> dict:
    d = dict(row)
    for k in ("expires_at", "redeemed_at", "created_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


# ─── Crew CRUD ───────────────────────────────────────────────────────


@router.get(
    "/api/boats/{boat_id}/crew",
    response_model=list[CrewMemberOut],
)
async def list_crew(
    boat_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Any member can see who's on the boat.

    Returns owner first, then crew, then viewer; within a role
    members are sorted by ``joined_at`` so the order matches the boat's
    natural history. We LEFT JOIN ``user_profiles`` so a freshly-
    redeemed invite (whose owner hasn't yet hit any endpoint to
    trigger the auth UPSERT) still shows up — just without a name
    until they next log in.
    """
    async with pool.acquire() as conn:
        await _require_boat_member(conn, boat_id, user["uid"])
        rows = await conn.fetch(
            """
            SELECT bc.user_id, bc.role, bc.joined_at,
                   up.email, up.display_name, up.avatar_url
            FROM boat_crew bc
            LEFT JOIN user_profiles up ON up.id = bc.user_id
            WHERE bc.boat_id = $1
            ORDER BY
                CASE bc.role
                    WHEN 'owner' THEN 0
                    WHEN 'crew'  THEN 1
                    WHEN 'viewer' THEN 2
                END,
                bc.joined_at ASC
            """,
            boat_id,
        )
    return [
        CrewMemberOut(
            user_id=r["user_id"],
            email=r["email"],
            display_name=r["display_name"],
            avatar_url=r["avatar_url"],
            role=r["role"],
            joined_at=r["joined_at"].isoformat(),
        )
        for r in rows
    ]


@router.patch(
    "/api/boats/{boat_id}/crew/{member_uid}",
    response_model=CrewMemberOut,
)
async def update_member_role(
    boat_id: UUID,
    member_uid: str,
    payload: CrewRoleUpdate,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Owner changes a member's role between 'crew' and 'viewer'.
    Cannot patch owners (the role check below excludes them)."""
    async with pool.acquire() as conn:
        await _require_boat_owner(conn, boat_id, user["uid"])
        row = await conn.fetchrow(
            """
            UPDATE boat_crew bc
            SET role = $3
            FROM (SELECT 1) _
            WHERE bc.boat_id = $1
              AND bc.user_id = $2
              AND bc.role IN ('crew', 'viewer')
            RETURNING bc.user_id, bc.role, bc.joined_at,
                      (SELECT email FROM user_profiles WHERE id = bc.user_id) AS email,
                      (SELECT display_name FROM user_profiles WHERE id = bc.user_id) AS display_name,
                      (SELECT avatar_url FROM user_profiles WHERE id = bc.user_id) AS avatar_url
            """,
            boat_id, member_uid, payload.role,
        )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "member not found or is the owner",
        )
    return CrewMemberOut(
        user_id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        avatar_url=row["avatar_url"],
        role=row["role"],
        joined_at=row["joined_at"].isoformat(),
    )


@router.delete(
    "/api/boats/{boat_id}/crew/{member_uid}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    boat_id: UUID,
    member_uid: str,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Owner removes a crew/viewer. Cannot remove themselves or
    another owner — those go through delete-boat or a future
    transfer-ownership flow."""
    async with pool.acquire() as conn:
        await _require_boat_owner(conn, boat_id, user["uid"])
        result = await conn.execute(
            """
            DELETE FROM boat_crew
            WHERE boat_id = $1
              AND user_id = $2
              AND role IN ('crew', 'viewer')
            """,
            boat_id, member_uid,
        )
    if result.rsplit(" ", 1)[-1] == "0":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "member not found or is the owner",
        )
    return None


# ─── Invites ─────────────────────────────────────────────────────────


@router.get(
    "/api/boats/{boat_id}/invites",
    response_model=list[InviteOut],
)
async def list_invites(
    boat_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Owner-only: list pending invites (not redeemed AND not expired)."""
    async with pool.acquire() as conn:
        await _require_boat_owner(conn, boat_id, user["uid"])
        rows = await conn.fetch(
            """
            SELECT id, boat_id, role, code, email, single_use,
                   expires_at, redeemed_at, created_at
            FROM boat_invites
            WHERE boat_id = $1
              AND redeemed_at IS NULL
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC
            """,
            boat_id,
        )
    return [
        InviteOut(
            **_format_invite_row(r),
            accept_url=_build_accept_url(r["code"]),
            emailed=False,  # historical sends not tracked beyond row creation
        )
        for r in rows
    ]


@router.post(
    "/api/boats/{boat_id}/invites",
    response_model=InviteOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    boat_id: UUID,
    payload: InviteCreate,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Owner creates an invite. Two flavours:

      * email set    → single-use UUID token, 7-day expiry by default,
                       sent via SendGrid (or skipped + URL returned).
      * email unset  → short multi-use join code, no expiry by default.

    Either way the response contains ``accept_url`` and an ``emailed``
    flag the frontend uses to decide whether to surface the URL for
    the owner to share manually.
    """
    async with pool.acquire() as conn:
        await _require_boat_owner(conn, boat_id, user["uid"])

        # Boat name for the email body — fetch alongside owner verify.
        boat_row = await conn.fetchrow(
            "SELECT name FROM boats WHERE id = $1", boat_id,
        )
        boat_name = boat_row["name"] if boat_row else "your boat"

        is_email = payload.email is not None
        if is_email:
            code = _generate_email_token()
            single_use = True
            expires_in = payload.expires_in_days or _DEFAULT_INVITE_EXPIRY_DAYS
            expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in)
        else:
            # Retry once on a UNIQUE collision (extremely rare).
            code = _generate_join_code()
            single_use = False
            expires_at = None
            if payload.expires_in_days:
                expires_at = (
                    datetime.now(timezone.utc)
                    + timedelta(days=payload.expires_in_days)
                )

        try:
            row = await conn.fetchrow(
                """
                INSERT INTO boat_invites (
                    boat_id, role, code, email, single_use,
                    created_by, expires_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, boat_id, role, code, email, single_use,
                          expires_at, redeemed_at, created_at
                """,
                boat_id, payload.role, code, payload.email,
                single_use, user["uid"], expires_at,
            )
        except asyncpg.UniqueViolationError:
            # One retry for join-code collisions; UUID tokens are
            # effectively unique so this only triggers for codes.
            if is_email:
                raise
            code = _generate_join_code()
            row = await conn.fetchrow(
                """
                INSERT INTO boat_invites (
                    boat_id, role, code, email, single_use,
                    created_by, expires_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, boat_id, role, code, email, single_use,
                          expires_at, redeemed_at, created_at
                """,
                boat_id, payload.role, code, payload.email,
                single_use, user["uid"], expires_at,
            )

    # Build accept_url from the row's code so the field always
    # matches the response's ``code`` (the local ``code`` variable
    # could diverge if a retry happened above).
    accept_url = _build_accept_url(row["code"])
    emailed = False
    if is_email:
        emailed = send_boat_invite(
            to_email=str(payload.email),
            boat_name=boat_name,
            owner_name=user.get("email") or user["uid"],
            accept_url=accept_url,
            role=payload.role,
        )
    return InviteOut(
        **_format_invite_row(row),
        accept_url=accept_url,
        emailed=emailed,
    )


@router.delete(
    "/api/boats/{boat_id}/invites/{code}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_invite(
    boat_id: UUID,
    code: str,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    async with pool.acquire() as conn:
        await _require_boat_owner(conn, boat_id, user["uid"])
        result = await conn.execute(
            "DELETE FROM boat_invites WHERE boat_id = $1 AND code = $2",
            boat_id, code,
        )
    if result.rsplit(" ", 1)[-1] == "0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    return None


@router.post(
    "/api/invites/redeem",
    response_model=InviteRedeemed,
)
async def redeem_invite(
    payload: InviteRedeem,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Caller redeems an invite by code.

    Statuses:
      * 404 — code doesn't exist
      * 410 — expired
      * 409 — single-use and already redeemed (or caller already a member)
      * 200 — boat_crew row created (or already present at the same role)
    """
    async with pool.acquire() as conn:
        invite = await conn.fetchrow(
            """
            SELECT id, boat_id, role, single_use,
                   expires_at, redeemed_at
            FROM boat_invites
            WHERE code = $1
            """,
            payload.code,
        )
        if invite is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
        if invite["expires_at"] is not None and invite["expires_at"] <= datetime.now(timezone.utc):
            raise HTTPException(status.HTTP_410_GONE, "invite expired")
        if invite["single_use"] and invite["redeemed_at"] is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "invite already redeemed",
            )

        # Already a member? Don't fail — idempotent redeem is friendlier
        # than 409 in the "I clicked the link twice" case. We still
        # return 200 and surface the existing role.
        existing = await conn.fetchrow(
            "SELECT role FROM boat_crew WHERE boat_id = $1 AND user_id = $2",
            invite["boat_id"], user["uid"],
        )
        if existing is not None:
            if invite["single_use"]:
                # Mark the invite redeemed so it can't be reused by
                # someone else if it leaks.
                await conn.execute(
                    "UPDATE boat_invites SET redeemed_at = NOW(), "
                    "redeemed_by = $2 WHERE id = $1",
                    invite["id"], user["uid"],
                )
            return InviteRedeemed(
                boat_id=invite["boat_id"], role=existing["role"],
            )

        # Create the membership row.
        await conn.execute(
            """
            INSERT INTO boat_crew (boat_id, user_id, role)
            VALUES ($1, $2, $3)
            """,
            invite["boat_id"], user["uid"], invite["role"],
        )
        if invite["single_use"]:
            await conn.execute(
                "UPDATE boat_invites SET redeemed_at = NOW(), "
                "redeemed_by = $2 WHERE id = $1",
                invite["id"], user["uid"],
            )

    return InviteRedeemed(boat_id=invite["boat_id"], role=invite["role"])
