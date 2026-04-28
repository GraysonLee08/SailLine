"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.routers import health


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage shared resources for the app's lifetime.

    Currently: the Cloud SQL connection pool. As more services are added
    (Redis, Firebase admin, etc.) initialize them here too.
    """
    await db.startup()
    try:
        yield
    finally:
        await db.shutdown()


app = FastAPI(
    title="SailLine API",
    description="Real-time race routing for sailors.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)


@app.get("/")
async def root():
    """Root endpoint — returns a hello message and confirms cfgrib is importable."""
    # Importing cfgrib here (rather than at module top) lets the app start even
    # if eccodes is missing. The /health endpoint will report the failure clearly.
    try:
        import cfgrib

        cfgrib_status = f"available (cfgrib {cfgrib.__version__})"
    except ImportError as e:
        cfgrib_status = f"unavailable: {e}"

    return {
        "service": "sailline-api",
        "version": "0.1.0",
        "cfgrib": cfgrib_status,
    }