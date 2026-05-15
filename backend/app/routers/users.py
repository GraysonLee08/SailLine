"""User-related endpoints.

All routes here require a valid Firebase ID token via the
``get_current_user`` dependency.

History:

  * **D2** — added ``default_boat_id`` echo on GET and a clearing-aware
    PATCH so RaceEditor can pre-select the user's boat.
  * **D4** — turned ``user_profiles`` into a real profile row:
    display_name, email, profile_complete, phone, bio, avatar_url,
    plus the sailing-and-safety bundle (weight_lb, emergency contact,
    World Sailing sailor ID/category, Safety-at-Sea cert expiry).
    Avatar upload/delete endpoints land here too.

PATCH semantics
---------------

The patch endpoint uses ``model_dump(exclude_unset=True)`` so that
**only keys the client actually sent** appear in the SET clause. An
omitted key means "leave it alone"; an explicit ``null`` value means
"clear it". This fixes a pre-D4 latent bug where omitting
``default_boat_id`` silently cleared it.

Setting a non-empty ``display_name`` also flips ``profile_complete``
to TRUE — that's the one and only way out of the forced-ProfileView
state for email-only sign-ups.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional
from uuid import UUID

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, field_validator

from app import db
from app.auth import get_current_user
from app.services.avatars import (
    AvatarProcessingError,
    delete_avatar as gcs_delete_avatar,
    process_avatar,
    store_avatar,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


# ─── Models ──────────────────────────────────────────────────────────


class UserProfileOut(BaseModel):
    """Returned by GET /me and the PATCH endpoint.

    All optional fields are echoed back regardless of whether the
    client set them this request — the frontend uses the full echo to
    re-render the ProfileView without a second round-trip.
    """

    uid: str
    email: Optional[str] = None
    tier: str
    default_boat_id: Optional[UUID] = None

    # D4: identity
    display_name: Optional[str] = None
    profile_complete: bool = False
    phone: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None

    # D4: sailing & safety
    weight_lb: Optional[float] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    world_sailing_sailor_id: Optional[str] = None
    world_sailing_category: Optional[
        Literal["group_1", "group_2", "group_3"]
    ] = None
    safety_at_sea_cert_expiry: Optional[date] = None


class UserProfilePatch(BaseModel):
    """All fields optional. Use exclude_unset=True on dump.

    Validation rules per field:

      * ``display_name`` — when sent, must be non-empty after strip.
        Triggers ``profile_complete = TRUE``.
      * ``bio`` — capped at 1000 chars (UX, not a SQL constraint).
      * ``phone`` / ``emergency_contact_phone`` — free-form text;
        we accept what the user types (formats vary by country) and
        only normalise enough to strip surrounding whitespace.
      * ``weight_lb`` — 50–500 lb. Catches typos like grams or kg.
      * ``world_sailing_category`` — DB has a CHECK; we mirror it via
        a Literal so we 422 before hitting the DB.
      * ``safety_at_sea_cert_expiry`` — plain date. No "must be in the
        future" rule — the user might enter a recently-expired cert
        for compliance records, and the frontend warns separately.
    """

    default_boat_id: Optional[UUID] = None
    display_name: Optional[str] = Field(default=None)
    phone: Optional[str] = None
    bio: Optional[str] = Field(default=None, max_length=1000)
    weight_lb: Optional[float] = Field(default=None, ge=50, le=500)
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    world_sailing_sailor_id: Optional[str] = Field(default=None, max_length=32)
    world_sailing_category: Optional[
        Literal["group_1", "group_2", "group_3"]
    ] = None
    safety_at_sea_cert_expiry: Optional[date] = None

    @field_validator("display_name")
    @classmethod
    def _display_name_nonempty(cls, v: Optional[str]) -> Optional[str]:
        # When the client sends ``display_name``, it must be a real
        # name. Setting it to "" or whitespace is treated as a user
        # error rather than a clear (clearing would un-complete the
        # profile, which would trap them in the forced-view loop).
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("display_name must be non-empty")
        if len(stripped) > 80:
            raise ValueError("display_name too long (max 80)")
        return stripped

    @field_validator("phone", "emergency_contact_phone", "emergency_contact_name")
    @classmethod
    def _trim(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None  # treat "   " as a clear

    @field_validator("world_sailing_sailor_id")
    @classmethod
    def _trim_sailor_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("bio")
    @classmethod
    def _bio_strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


# Columns we project out of ``user_profiles`` on every read. Defining
# this once keeps GET/PATCH/avatar in sync — adding a column means
# editing one tuple.
_PROFILE_COLUMNS = (
    "default_boat_id",
    "display_name",
    "profile_complete",
    "phone",
    "bio",
    "avatar_url",
    "weight_lb",
    "emergency_contact_name",
    "emergency_contact_phone",
    "world_sailing_sailor_id",
    "world_sailing_category",
    "safety_at_sea_cert_expiry",
    "email",
)
_PROFILE_COLUMNS_SQL = ", ".join(_PROFILE_COLUMNS)


def _row_to_out(uid: str, tier: str, row: asyncpg.Record | None) -> UserProfileOut:
    """Map a DB row → UserProfileOut. Handles the missing-row case
    (shouldn't happen since ``_ensure_profile`` UPSERTs on every auth
    check) by returning a minimal profile."""
    if row is None:
        return UserProfileOut(uid=uid, tier=tier)
    weight = row["weight_lb"]
    if isinstance(weight, Decimal):
        weight = float(weight)
    return UserProfileOut(
        uid=uid,
        tier=tier,
        # ``email`` lives in the DB now; that's authoritative over the
        # token claim (claim is identical in practice but the column
        # is the source of truth we let users see in ProfileView).
        email=row["email"],
        default_boat_id=row["default_boat_id"],
        display_name=row["display_name"],
        profile_complete=row["profile_complete"],
        phone=row["phone"],
        bio=row["bio"],
        avatar_url=row["avatar_url"],
        weight_lb=weight,
        emergency_contact_name=row["emergency_contact_name"],
        emergency_contact_phone=row["emergency_contact_phone"],
        world_sailing_sailor_id=row["world_sailing_sailor_id"],
        world_sailing_category=row["world_sailing_category"],
        safety_at_sea_cert_expiry=row["safety_at_sea_cert_expiry"],
    )


# ─── GET /me ─────────────────────────────────────────────────────────


@router.get("/me", response_model=UserProfileOut)
async def me(
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> UserProfileOut:
    """Return the authenticated user's full profile."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_PROFILE_COLUMNS_SQL} FROM user_profiles WHERE id = $1",
            user["uid"],
        )
    return _row_to_out(user["uid"], user["tier"], row)


# ─── PATCH /me ───────────────────────────────────────────────────────


# Whitelist of columns the PATCH endpoint will write. Anything not in
# here is ignored even if the Pydantic model parses it (defense in
# depth against accidentally exposing a new field via a schema-only
# change).
_PATCHABLE_COLUMNS = frozenset(
    {
        "default_boat_id",
        "display_name",
        "phone",
        "bio",
        "weight_lb",
        "emergency_contact_name",
        "emergency_contact_phone",
        "world_sailing_sailor_id",
        "world_sailing_category",
        "safety_at_sea_cert_expiry",
    }
)


@router.patch("/me", response_model=UserProfileOut)
async def update_me(
    payload: UserProfilePatch,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> UserProfileOut:
    """Partial update.

    Only keys the client sent end up in the SET clause; omitted keys
    are left alone. Sending an explicit ``null`` for a clearable field
    sets it to NULL.

    Side effects:

      * ``default_boat_id`` (when sent non-null) is verified to belong
        to a boat the caller can read (owner or crew member).
      * ``display_name`` (when sent) flips ``profile_complete`` to TRUE.
    """
    data: dict[str, Any] = payload.model_dump(exclude_unset=True)
    # Drop any keys the model parsed but the whitelist forbids. In
    # practice the model only carries patchable fields, so this is
    # just future-proofing.
    data = {k: v for k, v in data.items() if k in _PATCHABLE_COLUMNS}

    async with pool.acquire() as conn:
        # default_boat_id ownership/membership check before any write.
        if "default_boat_id" in data and data["default_boat_id"] is not None:
            from app.auth_helpers import boat_read_predicate

            pred = boat_read_predicate(boat_alias="b", uid_placeholder="$2")
            owns = await conn.fetchrow(
                f"SELECT 1 FROM boats b WHERE b.id = $1 AND {pred}",
                data["default_boat_id"], user["uid"],
            )
            if owns is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "default boat not found among your boats",
                )

        if data:
            # Build a dynamic SET clause: $1..$N for the column values,
            # then $N+1 for the WHERE uid. Order is stable thanks to
            # the dict's insertion-preserving semantics on Python ≥ 3.7
            # (and we sort below anyway for test-friendly determinism).
            cols = sorted(data.keys())
            assignments = [f"{col} = ${i + 1}" for i, col in enumerate(cols)]
            # display_name being set implies profile_complete=TRUE.
            # We OR rather than overwrite so re-saving an unchanged
            # profile doesn't accidentally un-complete it.
            if "display_name" in data and data["display_name"] is not None:
                assignments.append("profile_complete = TRUE")
            sql = (
                f"UPDATE user_profiles SET {', '.join(assignments)} "
                f"WHERE id = ${len(cols) + 1}"
            )
            args = [data[c] for c in cols] + [user["uid"]]
            await conn.execute(sql, *args)

        row = await conn.fetchrow(
            f"SELECT {_PROFILE_COLUMNS_SQL} FROM user_profiles WHERE id = $1",
            user["uid"],
        )

    return _row_to_out(user["uid"], user["tier"], row)


# ─── Avatar upload / delete ──────────────────────────────────────────


@router.post("/me/avatar", response_model=UserProfileOut)
async def upload_avatar(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> UserProfileOut:
    """Accept an image, resize to 256×256 WebP, upload to GCS.

    Errors:
      * 400 — unreadable / unsupported / empty image
      * 413 — file larger than the size cap
      * 503 — bucket not configured (dev-only path; prod always sets it)

    The avatar_url written to the DB carries a ``?v={epoch}`` cache-
    buster so the frontend's just-uploaded preview displaces any
    browser-cached version of the previous file.
    """
    contents = await file.read()
    try:
        processed = process_avatar(contents, file.content_type)
    except AvatarProcessingError as exc:
        # Size-overflow is a distinct status; everything else is a 400.
        msg = str(exc)
        if "too large" in msg:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, msg
            ) from exc
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg) from exc

    stored_url = store_avatar(processed, user["uid"])
    if stored_url is None:
        # No bucket configured in this environment. Fail visibly
        # rather than silently — a user trying to upload an avatar in
        # dev should be told the feature is gated on infra.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "avatar storage not configured",
        )

    # Cache-busting query string. Two uploads in the same second land
    # at the same URL → harmless, the bytes are also the same in
    # practice (overwritten blob). Different uploads → guaranteed
    # different URL → browser refetches.
    import time

    busted_url = f"{stored_url}?v={int(time.time())}"

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_profiles SET avatar_url = $1 WHERE id = $2",
            busted_url, user["uid"],
        )
        row = await conn.fetchrow(
            f"SELECT {_PROFILE_COLUMNS_SQL} FROM user_profiles WHERE id = $1",
            user["uid"],
        )
    return _row_to_out(user["uid"], user["tier"], row)


@router.delete("/me/avatar", response_model=UserProfileOut)
async def delete_avatar(
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
) -> UserProfileOut:
    """Clear the user's avatar. DB write is authoritative; the GCS
    delete is best-effort (failure leaves an orphan blob the next
    upload will overwrite anyway)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_profiles SET avatar_url = NULL WHERE id = $1",
            user["uid"],
        )
        row = await conn.fetchrow(
            f"SELECT {_PROFILE_COLUMNS_SQL} FROM user_profiles WHERE id = $1",
            user["uid"],
        )
    gcs_delete_avatar(user["uid"])
    return _row_to_out(user["uid"], user["tier"], row)
