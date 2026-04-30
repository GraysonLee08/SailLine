"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import auth, db, redis_client
from app.routers import health, races, users, weather


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage shared resources for the app's lifetime.

    Currently: Firebase Admin SDK, Cloud SQL connection pool, Redis client.
    As more services are added, initialize them here too.
    """
    auth.initialize()
    await db.startup()
    await redis_client.startup()
    try:
        yield
    finally:
        await redis_client.shutdown()
        await db.shutdown()


app = FastAPI(
    title="SailLine API",
    description="Real-time race routing for sailors.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow the React dev server and the deployed Firebase Hosting
# origins to call the API. The pattern catches both `sailline.web.app`
# and `sailline.firebaseapp.com`, plus any future *.web.app preview
# channels Firebase Hosting may issue. Add a custom domain here when
# we register one (e.g. `https://sailline.app`).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5000",  # firebase emulator default
        "https://sailline.web.app",
        "https://sailline.firebaseapp.com",
    ],
    allow_origin_regex=r"https://sailline--.*\.web\.app",  # preview channels
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health.router)
app.include_router(users.router, prefix="/api")
app.include_router(races.router, prefix="/api")
app.include_router(weather.router)  # router carries its own /api/weather prefix


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
