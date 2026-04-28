"""User-related endpoints.

All routes here require a valid Firebase ID token via the
`get_current_user` dependency.
"""

from fastapi import APIRouter, Depends

from app.auth import get_current_user

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def me(user: dict = Depends(get_current_user)) -> dict:
    """Return the authenticated user's profile.

    The `claims` field surfaces the raw Firebase token claims for debugging;
    feel free to drop it once the frontend is wired up and you no longer
    need to inspect what the token carries.
    """
    return {
        "uid": user["uid"],
        "email": user["email"],
        "tier": user["tier"],
        "claims": user["claims"],
    }
