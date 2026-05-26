# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

SailLine is a map-first race routing app for Great Lakes sailors. Frontend is React + Vite on Firebase Hosting; backend is FastAPI on Cloud Run with Cloud SQL (PostGIS) + Memorystore Redis on a private VPC. NOAA HRRR/GFS GRIB2 data is parsed by Cloud Run Jobs and cached to Redis (with GCS as a durability/fallback archive).

## Common commands

### Backend (run from `backend/`)

```bash
# venv setup
python -m venv .venv && .\.venv\Scripts\Activate.ps1     # PowerShell
pip install -r requirements.txt

# Run the API locally
uvicorn app.main:app --reload --port 8080                 # docs at /docs

# Tests
pytest                                                     # pytest.ini sets pythonpath=.
pytest tests/test_isochrone_engine.py -v                   # single file
pytest tests/test_navigability.py::test_name -v            # single test
pytest -m slow                                             # only slow/network tests
pytest -m "not slow"                                       # skip slow tests (default for CI-style runs)

# One-time DB bootstrap on a fresh database (PostGIS + role grants + ownership).
# Run as the postgres superuser BEFORE the first alembic upgrade:
psql -h 127.0.0.1 -U postgres -d sailline_app -f ../infra/schema.sql

# Migrations (Alembic — must be run from backend/)
alembic upgrade head
alembic current
alembic revision -m "short_description"
alembic downgrade -1                                       # dev only

# Weather ingest worker (also runs as Cloud Run Job in prod)
python -m workers.weather_ingest hrrr --region conus --dry-run
python -m workers.weather_ingest gfs  --region conus --dry-run
```

### Frontend (run from `frontend/`)

```bash
npm install
npm run dev          # http://localhost:5173 — set VITE_API_URL=http://localhost:8080 in .env.local
npm run build        # → dist/
npm run deploy       # build + firebase deploy --only hosting
```

`npm test` runs vitest (one-shot) and is gated in CI via `infra/cloudbuild.frontend.yaml` — a failing test blocks deploy. No `lint` script is configured.

### Mobile (run from `mobile/`) — Expo RN app

```bash
npx expo install            # reconcile deps to the SDK (authoritative versioner)
npx expo start --dev-client # Metro for the custom dev client (NOT Expo Go)

# Custom dev client is required — Transistorsoft background-geolocation is a
# native module Expo Go can't load. Build it via EAS (runs under your account):
eas build --profile development --platform android   # installable .apk
eas build --profile development --platform ios       # cloud macOS, no Mac needed

# Background-location license lives in an EAS secret, never committed:
eas secret:create --name TRANSISTOR_LICENSE --value <key>
```

Local backend from a phone: set `EXPO_PUBLIC_API_URL=http://<LAN-ip>:8080`; it
defaults to the production Cloud Run origin. Native fetch sends no `Origin`
header, so backend CORS does not gate mobile requests. `app.config.js` extends
the static `app.json` with the dev-client + Transistorsoft plugins and the
iOS/Android background-location config.

### Deploy

Cloud Build auto-deploys on push to `main` — `infra/cloudbuild.yaml` (backend → Cloud Run) and `infra/cloudbuild.frontend.yaml` (frontend → Firebase Hosting). Both pipelines run tests before deploy (`pytest -m "not slow"` for backend, `npm test` for frontend) — a failing test blocks deploy. **Migrations are intentionally manual**; see `docs/migrations.md` for the runbook (short version: apply additive migrations *before* pushing; split destructive ones across two commits).

## Architecture

### Backend layout (`backend/`)

- `app/main.py` — FastAPI entrypoint; lifespan initializes Firebase Admin, asyncpg pool, Redis. Routers are mounted with their own prefixes.
- `app/auth.py` — Firebase ID token verification (`get_current_user`) plus `require_pro`/tier gating. UPSERTs the user into `user_profiles` on first call.
- `app/db.py` — Cloud SQL Connector → asyncpg pool over the VPC's private IP. Startup is **non-fatal**: pool failure leaves the app running and `/health` reports clearly; endpoints needing the DB return 503.
- `app/redis_client.py` — Same non-fatal pattern for Memorystore.
- `app/config.py` — pydantic-settings; reads `.env` locally, env vars (incl. Secret Manager-injected) on Cloud Run.
- `app/regions.py` — **Single source of truth** for wind regions. Two kinds: **base** (`conus` HRRR+GFS, `hawaii` GFS-only, always-on coverage) and **venue** (high-res HRRR overlays at native 0.027°, ~3 km, only at zoom ≥ 11 over the bbox). The frontend mirror lives at `frontend/src/lib/regions.js` — **edit both together**; region names are the public `/api/weather?region=...` contract.
- `app/routers/` — `health`, `users`, `weather`, `races`, `tracks`, `routing` (POST `/api/routing/compute`), `routing_notifications` (SSE for "better route" alerts).
- `app/services/` — domain logic. `routing/` (isochrone engine, wind forecast, navigability), `weather/` (forecast loader), `bathymetry/`, `charts/` (ENC hazards), `polars/` (boat polar CSVs), `boats.py`, `grib.py`.
- `workers/` — Cloud Run Job entrypoints: `weather_ingest.py` (NOAA HRRR/GFS → Redis + GCS, run hourly/6h via Cloud Scheduler), `bathymetry_ingest.py`, `enc_ingest.py` (load ENC hazard polygons to GCS), `route_recompute.py` (post-ingest "better route" detector that publishes to Redis pub/sub channels tailed by the SSE endpoint).
- `migrations/` — Alembic versions, raw SQL via `op.execute(...)` (no ORM). Sequential numbering: `0001`, `0002`, `0003`. `infra/schema.sql` only handles one-time bootstrap (PostGIS extension, role grants, ownership transfer for pre-Alembic tables) — **never add tables there**.
- `tests/conftest.py` — sets dummy DB env vars at import time so `app.config.Settings()` constructs cleanly without a real `.env`.

### Routing pipeline

`POST /api/routing/compute` flow (see `app/routers/routing.py`):
1. Resolve race ownership and load marks/`start_at`/boat class.
2. **Region resolution from marks centroid** → `(base_region, venue_or_None)`. Base drives wind+bathymetry; venue (when present) triggers harbour-scale ENC hazard loading.
3. Load polar (per boat class) + boat spec (draft, `min_depth_m = draft × safety_factor`).
4. `load_forecast_for_race` builds a time-aware `WindForecast` spanning the race window. Returns HTTP **425 Too Early** with `{available_at, hours_until_available}` when the cycle isn't out yet — the frontend reschedules a refetch.
5. `make_navigable_predicate(region, draft_m, safety_factor, venue)` returns a callable with a `.segment(lat1, lon1, lat2, lon2)` method (preferred line-vs-polygon check; falls back to per-point sampling for legacy callers).
6. `compute_isochrone_route` (pure-numpy, in `app/services/routing/isochrone.py`) iterates `dt_minutes` frontiers, sweeps headings, samples wind at simulated `valid_time = race_start + iter*dt`, applies polar + navigability segment check, prunes via Hagiwara bearing-bin culling.
7. Cache key includes `ENGINE_VERSION` (bump on any algorithm change), race_id, race_start, both cycle ids, snapshot sources, safety_factor, venue. Result stored in Redis with a 1h TTL.

The background `workers/route_recompute.py` runs the same pipeline (must use the same region/venue hazards as the sync endpoint, or "better route" alerts surface paths the user-facing route correctly avoided) and publishes to `route:notifications:{race_id}`. The SSE endpoint replays the latest stored alternative on connect so reconnecting clients don't miss state.

### Frontend layout (`frontend/src/`)

- `App.jsx` — auth gate. `AppView` is `lazy()` so the auth-gate path doesn't pull mapbox-gl.
- `AuthView.jsx` / `AppView.jsx` — login / post-login shell.
- `RaceEditor.jsx`, `RacesListView.jsx` — full-screen course editor + list.
- `components/MapView.jsx` — Mapbox GL canvas, wind barbs, course rendering. `BetterRouteBanner.jsx`, `RouteControls.jsx`.
- `hooks/` — `useWeather`, `useGeolocation`, `useCountdown`, `useRaces`, `useRegion`, `useRouting`, `useRouteNotifications` (uses `@microsoft/fetch-event-source` because the native `EventSource` API can't send `Authorization` headers — required for our auth model), `useTrackRecorder`.
- `lib/` — `latlon.js` (decimal + deg-decimal-min parsing), `morfMarks.js` + `morfCourses.js` (24 named MORF marks, 64 buoy course presets), `windBarb.js` (adaptive density barb generator), `regions.js` (mirror of `backend/app/regions.py`), `boatClasses.js`.
- `firebase.json` rewrites `/api/**` → Cloud Run `sailline-api` so production frontend calls are same-origin (no CORS preflight); dev uses `VITE_API_URL=http://localhost:8080`.

### Conventions worth knowing

- **No ORM** — raw SQL via asyncpg in routers, raw SQL via `op.execute()` in migrations. Keeps migrations readable and matches runtime exactly.
- JSONB codec is registered globally on every asyncpg connection (`app/db.py::_init_connection`) — pass/receive plain Python dicts, not strings, for JSONB columns.
- A failed migration mid-deploy is messier than a hand-applied known-state one — that's why migrations are decoupled from auto-deploy. After running a destructive migration, force a Cloud Run revision rollover with `--update-env-vars=BUMP=$(date +%s)` so asyncpg drops stale prepared statements.
- Region edits require touching `backend/app/regions.py` + `frontend/src/lib/regions.js` together. Tests in `test_regions`/ingest tests catch the mirror drift.
- ENGINE_VERSION (in `routing.py`) is part of the route cache key — bump it on any change to polar, mask, or algorithm to invalidate stale routes.
- Pre-Alembic tables (`user_profiles`, `race_sessions`) were created by `postgres` superuser; ownership has been transferred to `sailline`. Tables created by Alembic are owned by `sailline` automatically.
- Session-summary build log lives in `docs/YYYY-MM-DD-*.md` — useful as historical context for non-obvious decisions.
- `pytest.ini` sets `asyncio_mode = auto`, so async tests do **not** need `@pytest.mark.asyncio` — just `async def test_...`.

### Key docs

- `docs/prd.md` — product requirements
- `docs/architecture.md` — GCP architecture overview
- `docs/migrations.md` — Alembic runbook (read this before any destructive migration)
- `docs/2026-MM-DD-*.md` — chronological session summaries / build log
