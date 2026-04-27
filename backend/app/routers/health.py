"""Health check endpoints for Cloud Run probes and uptime monitoring."""

from fastapi import APIRouter, status
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    cfgrib_available: bool
    cfgrib_version: str | None = None
    cfgrib_error: str | None = None


@router.get("", response_model=HealthResponse, status_code=status.HTTP_200_OK)
async def health() -> HealthResponse:
    """Liveness probe.

    Verifies that cfgrib (and therefore the eccodes C library) is properly
    installed in the container. Used by Cloud Run startup probes and as a
    smoke test during local Docker validation.
    """
    try:
        import cfgrib

        return HealthResponse(
            status="ok",
            cfgrib_available=True,
            cfgrib_version=cfgrib.__version__,
        )
    except ImportError as e:
        return HealthResponse(
            status="degraded",
            cfgrib_available=False,
            cfgrib_error=str(e),
        )