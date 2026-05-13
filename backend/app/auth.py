"""Firebase Authentication — JWT verification and tier gating.

The frontend signs users in via the Firebase JS SDK, which yields a short-lived
ID token. The frontend includes that token on every API call as
`Authorization: Bearer <token>`. We verify the signature and freshness here
using the Firebase Admin SDK, then look up (or lazily create) the user's row
in `user_profiles` to get their subscription tier.

Two entry points share the same underlying verification:
  - `get_current_user`  HTTP route dependency. HTTPBearer reads the token
                        from the Authorization header; failures raise 401.
  - `verify_ws_token`   WebSocket-friendly. Accepts a raw token string
                        (browsers can't set Authorization on WS connections,
                        so the token rides on the URL query). Failures raise
                        InvalidTokenError; the WS handler is expected to
                        catch it and close with code 1008.

Both paths funnel through `_verify_token_string`, so any change to the
verification logic (revocation checks, custom-claims handling) applies to
HTTP and WS uniformly.

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


class InvalidTokenError(Exception):
    """Raised when a Firebase ID token fails verification.

    Carries a short reason string suitable for logging or surfacing in a
    WebSocket close frame. Each transport translates this to its own
    error shape:
      - HTTP: 401 with WWW-Authenticate header
      - WS:   close code 1008 (policy violation)
    """


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


async def _verify_token_string(token: str, pool: asyncpg.Pool) -> dict:
    """Core verification — token string in, user dict out.

    Called by both HTTP and WebSocket auth paths. Raises InvalidTokenError
    on any verification failure; the caller translates that to a
    transport-appropriate error response.

    `verify_id_token` is sync (it does an HTTP fetch + JWT verify), so we
    push it to a thread to avoid blocking the event loop.
    """
    try:
        decoded = await asyncio.to_thread(fb_auth.verify_id_token, token)
    except (
        fb_auth.InvalidIdTokenError,
        fb_auth.ExpiredIdTokenError,
        fb_auth.RevokedIdTokenError,
        ValueError,
    ) as exc:
        raise InvalidTokenError(type(exc).__name__) from exc

    uid = decoded["uid"]
    tier = await _ensure_profile(pool, uid)
    return {
        "uid": uid,
        "email": decoded.get("email"),
        "tier": tier,
        "claims": decoded,
    }


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_security),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    """Verify the bearer token and return the authenticated user.

    HTTP route dependency. Returns the same user dict shape as the WS
    path — `{uid, email, tier, claims}` — so downstream code is
    transport-agnostic.
    """
    try:
        return await _verify_token_string(creds.credentials, pool)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def verify_ws_token(token: str, pool: asyncpg.Pool) -> dict:
    """Verify a Firebase ID token passed as a WebSocket query parameter.

    The browser WebSocket API can't set Authorization headers, so tokens
    ride on the URL: `wss://.../path?token=<id_token>`. The short-lived
    (~1h) nature of Firebase ID tokens bounds the blast radius of
    incidental query-string exposure in server logs.

    Raises InvalidTokenError on any verification failure. The WS handler
    is expected to catch this and close the connection with code 1008.
    """
    return await _verify_token_string(token, pool)


def require_pro(user: dict = Depends(get_current_user)) -> dict:
    """Dependency: 403 unless the caller is on Pro or Hardware tier."""
    if user["tier"] not in ("pro", "hardware"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="pro subscription required",
        )
    return user