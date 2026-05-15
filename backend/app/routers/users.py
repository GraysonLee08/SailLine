"""User-related endpoints.

All routes here require a valid Firebase ID token via the
`get_current_user` dependency.

D2 additions:
  * GET /users/me now echoes ``default_boat_id`` so the frontend can
    pre-select the boat dropdown on RaceEditor.
  * PATCH /users/me lets the user set their default boat (or clear it
    by passing null).
"""

from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import db
from app.auth import get_current_user

router = APIRouter(prefix="/users", tags=["users"])


class UserProfileOut(BaseModel):
    uid: str
    email: Optional[str] = None
    tier: str
    default_boat_id: Optional[UUID] = None


class UserProfilePatch(BaseModel):
    # Use a sentinel-aware payload: a missing key means "leave it
    # alone"; an explicit null clears the default boat. We can't
    # distinguish "missing" from "null" once Pydantic parses, so we
    # accept both shapes and rely on the route to do the right thing.
    default_boat_id: Optional[UUID] = None


@router.get("/me", response_model=UserProfileOut)
async def me(
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> UserProfileOut:
    """Return the authenticated user's profile."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT default_boat_id FROM user_profiles WHERE id = $1",
            user["uid"],
        )
    default_boat_id = row["default_boat_id"] if row else None
    return UserProfileOut(
        uid=user["uid"],
        email=user.get("email"),
        tier=user["tier"],
        default_boat_id=default_boat_id,
    )


@router.patch("/me", response_model=UserProfileOut)
async def update_me(
    payload: UserProfilePatch,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> UserProfileOut:
    """Update profile fields. v1 only writes ``default_boat_id`` (incl.
    clearing it to NULL).

    Auth: the FK to boats has ON DELETE SET NULL, but we should also
    verify the boat actually belongs to this user before pointing the
    profile at it — otherwise users could "set" someone else's boat
    as their default and the join would surface that boat's rating on
    their stats. Cheap SELECT before the UPDATE handles it.
    """
    new_default = payload.default_boat_id
    async with pool.acquire() as conn:
        if new_default is not None:
            # D3: pre-select any boat the caller is a member of, not
            # just boats they own. A crew member who races on someone
            # else's boat can pick that boat as their default.
            from app.auth_helpers import boat_read_predicate
            pred = boat_read_predicate(boat_alias="b", uid_placeholder="$2")
            owns = await conn.fetchrow(
                f"SELECT 1 FROM boats b WHERE b.id = $1 AND {pred}",
                new_default, user["uid"],
            )
            if owns is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "default boat not found among your boats",
                )
        await conn.execute(
            "UPDATE user_profiles SET default_boat_id = $1 WHERE id = $2",
            new_default, user["uid"],
        )
    return UserProfileOut(
        uid=user["uid"],
        email=user.get("email"),
        tier=user["tier"],
        default_boat_id=new_default,
    )
