"""Firebase Authentication — JWT verification and tier gating.

The frontend signs users in via the Firebase JS SDK, which yields a short-lived
ID token. The frontend includes that token on every API call as
`Authorization: Bearer <token>`. We verify the signature and freshness here
using the Firebase Admin SDK, then look up (or lazily create) the user's row
in `user_profiles` to get their subscription tier.

Firebase Admin auto-discovers credentials on Cloud Run via the runtime
service account (`sailline-api`), which has the `firebaseauth.admin` role.
No JSON key file or env var is required in production.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg
import firebase_admin
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as fb_auth

from app.db import get_pool

log = logging.getLogger(__name__)

_security = HTTPBearer(auto_error=True, description="Firebase ID token")


def initialize() -> None:
    """Idempotent Firebase Admin initialization.

    Called once at app startup from the lifespan context. Safe to call again;
    `firebase_admin.initialize_app()` raises if an app is already registered,
    so we guard with the module-level registry.
    """
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
        log.info("Firebase Admin initialized")


async def _ensure_profile(pool: asyncpg.Pool, uid: str) -> str:
    """Return the user's tier, lazily creating their profile row if missing.

    Uses an UPSERT with a no-op ON CONFLICT clause so the RETURNING projection
    always yields a row, in a single round trip and without race conditions.
    """
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO user_profiles (id) VALUES ($1)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING tier
            """,
            uid,
        )


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_security),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    """Verify the bearer token and return the authenticated user.

    Steps:
    1. Verify the Firebase ID token (signature, expiry, issuer, audience).
    2. UPSERT the user's profile row in Postgres, fetching their tier.
    3. Return a dict with the uid, optional email, tier, and full claims.

    `verify_id_token` is sync (it does an HTTP fetch + JWT verify), so we
    push it to a thread to avoid blocking the event loop.
    """
    try:
        decoded = await asyncio.to_thread(fb_auth.verify_id_token, creds.credentials)
    except (
        fb_auth.InvalidIdTokenError,
        fb_auth.ExpiredIdTokenError,
        fb_auth.RevokedIdTokenError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {type(exc).__name__}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    uid = decoded["uid"]
    tier = await _ensure_profile(pool, uid)

    return {
        "uid": uid,
        "email": decoded.get("email"),
        "tier": tier,
        "claims": decoded,
    }


def require_pro(user: dict = Depends(get_current_user)) -> dict:
    """Dependency: 403 unless the caller is on Pro or Hardware tier."""
    if user["tier"] not in ("pro", "hardware"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="pro subscription required",
        )
    return user
