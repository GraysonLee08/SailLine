"""FastAPI application entry point."""

from fastapi import FastAPI

from app.routers import health

app = FastAPI(
    title="SailLine API",
    description="Real-time race routing for sailors.",
    version="0.1.0",
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