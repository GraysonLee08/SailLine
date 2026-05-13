# SailLine — Project Status

**Last updated:** Week 1 infrastructure complete · Step A (Cloud SQL wiring) complete
**Status:** ✅ Backend deployed, DB roundtrip verified · ready for Step B (Firebase auth)

---

## Executive Summary

You've moved from "idea" to "production-deployed backend with a verified end-to-end database roundtrip" in a single sprint. Everything below is provisioned, documented, and working:

- **Product strategy** captured in a 1.1 PRD with validated cost model (break-even at 22 Pro subscribers)
- **Technical architecture** designed for solo developer on GCP with auto-deploy CI/CD
- **GitHub repo** with full structure, README, PRD, architecture, and seven infrastructure docs
- **GCP infrastructure** fully provisioned: VPC, Cloud SQL with PostGIS, Memorystore Redis, Cloud Storage, Artifact Registry, Cloud Build, Cloud Run
- **First production deployment** of FastAPI backend with `cfgrib` weather library validated working
- **Auto-deploy pipeline** wired up: every `git push` to `main` triggers a build and ships a new revision
- **Cloud SQL connectivity proven**: FastAPI on Cloud Run successfully queries Postgres + PostGIS through the VPC connector via the Cloud SQL Python Connector

The two hardest technical risks in the project (`cfgrib`/`eccodes` working in a deployed container, and Cloud Run reaching Cloud SQL through the VPC) are now retired. The path from here is application code.

---

## 1. Product Definition

**SailLine** is a real-time race routing web app for sailors. Combines NOAA wind forecasts, AIS competitor tracking, and boat-specific polar diagrams to recommend optimal race routes — both before the start and continuously during the race. An AI tactical advisor (Claude) translates routing math into plain-language guidance so non-expert sailors can use it.

### Target

- **Geography:** US Great Lakes at launch
- **Primary user:** Club racer (Beneteau 36.7, J/105, etc.) racing MORF + Chicago-Mac
- **Secondary:** Distance racer doing Mac and similar overnighters
- **Tertiary:** Boat owner who installs the Pi telemetry kit (v2)

### Tier structure

| Tier | Monthly | Annual | Gates |
|---|---|---|---|
| **Free** | $0 | — | Pre-race planning · all boat classes · 24hr weather · no AIS |
| **Pro** | $15/mo | $149/yr | In-race routing · AIS · 7-day weather · AI advisor · handicap support · GPS track recording |
| **Hardware** | $25/mo | $249/yr | Pro + Pi telemetry · current detection · learned polars · instrument-enhanced post-race analysis. Requires ~$200 hardware kit. |

### Boat classes at launch

Beneteau First 36.7 (P0) · J/105 · J/109 · J/111 · Farr 40 · Beneteau First 40.7 · Tartan 10 · Generic PHRF/ORC

### Handicap systems

PHRF · ORC · ORR-EZ · IRC · MORF

### Three-layer routing differentiator

1. **Isochrone routing** as the algorithmic foundation (industry standard)
2. **Probabilistic ensemble routing** (v1.5) — running 21 NOAA GEFS forecast members instead of one deterministic forecast
3. **ML-learned polars** (v3) — neural network trained on real instrument telemetry per-boat

---

## 2. Build Sequence

- **Race modes:** inshore (recalc every 2–3 min, AIS every 3 min) vs distance (recalc every 10–15 min, AIS every 5 min).

| Week | Focus |
|---|---|
| **1** | Infrastructure setup ✅ + Step A (Cloud SQL wiring) ✅ |
| **2** | Weather pipeline (NOAA → cfgrib → Redis) |
| **3–4** | Isochrone routing engine (the hardest piece — prototype as standalone script first) |
| **5** | Pre-race UI (map, mark placement, route overlay) |
| **6** | In-race routing (SSE stream, GPS hook, recalculation cadence) |
| **7** | AIS + Stripe (competitor map, subscription gating) |
| **8** | AI advisor + GPS track recording |
| **9** | Polars + handicap support (all 8 classes) |
| **10** | Polish + Mac season launch |

---

## 3. Infrastructure Provisioned

Everything below is live and verified working in the `sailline` GCP project.

### APIs enabled

```
run.googleapis.com
sqladmin.googleapis.com
cloudbuild.googleapis.com
artifactregistry.googleapis.com
secretmanager.googleapis.com
vpcaccess.googleapis.com
cloudscheduler.googleapis.com
servicenetworking.googleapis.com
redis.googleapis.com
```

### Network

| Resource | Identifier | Notes |
|---|---|---|
| VPC | `default` | GCP-managed default VPC |
| IP allocation for managed services | `google-managed-services-default` | `/16`, RESERVED, VPC_PEERING purpose |
| VPC Peering | `servicenetworking-googleapis-com` | Connects to Google managed services VPC |
| Serverless VPC Connector | `sailline-connector` | Region: `us-central1`, range `10.8.0.0/28`, state READY |

### Database

| Resource | Identifier | Notes |
|---|---|---|
| Cloud SQL instance | `sailline-db` | PostgreSQL 15.17, region `us-central1`, db-f1-micro, dual public+private IP |
| Private IP | `10.69.0.3` | Used by Cloud Run via VPC connector |
| Database | `sailline_app` | App database |
| Admin user | `postgres` | Password in Secret Manager |
| App user | `sailline` | Password in Secret Manager |
| Extensions installed | `postgis` 3.6, `postgis_topology` | Verified live via FastAPI roundtrip |

Tables: `user_profiles` (created); `race_sessions`, `track_points`, `telemetry_points` (deferred to their respective build weeks).

### Cache

| Resource | Identifier | Notes |
|---|---|---|
| Memorystore Redis | `sailline-cache` | Basic tier, 1GB, region `us-central1`, redis_7_2 |
| Private IP | `10.69.1.3` | Used by Cloud Run via VPC connector |
| Connect mode | `PRIVATE_SERVICE_ACCESS` | Reuses VPC peering |

### Object storage

| Resource | Identifier | Notes |
|---|---|---|
| Bucket | `gs://sailline-weather` | Region `us-central1`, uniform bucket-level access, public access prevented |
| Lifecycle rule | Delete objects > 30 days | Auto-cleanup of old GRIB2 files |

### Identity & access

| Service Account | Email | Roles |
|---|---|---|
| API runtime | `sailline-api@sailline.iam.gserviceaccount.com` | `cloudsql.client`, `secretmanager.secretAccessor`, `firebaseauth.admin`, bucket-scoped `storage.objectAdmin`, repo-scoped `artifactregistry.reader` |
| Cloud Build | `sailline-cloudbuild@sailline.iam.gserviceaccount.com` | `artifactregistry.writer`, `run.admin`, `logging.logWriter`, SA-scoped `iam.serviceAccountUser` on `sailline-api` |

### Secrets in Secret Manager

| Secret name | Status |
|---|---|
| `sailline-db-postgres-password` | ✅ created |
| `sailline-db-app-password` | ✅ created · injected into Cloud Run via `--set-secrets` |
| `sailline-datalastic-api-key` | ⏸ deferred to Week 7 |
| `sailline-anthropic-api-key` | ⏸ deferred to Week 8 |
| `sailline-stripe-secret-key` | ⏸ deferred to Week 7 |
| `sailline-stripe-webhook-secret` | ⏸ deferred to Week 7 |

### Container infrastructure

| Resource | Identifier | Notes |
|---|---|---|
| Artifact Registry repo | `sailline` | Region `us-central1`, format DOCKER |
| Image URL pattern | `us-central1-docker.pkg.dev/sailline/sailline/api:<tag>` | Tagged with `$SHORT_SHA` and `latest` |

### CI/CD pipeline

| Resource | Identifier | Notes |
|---|---|---|
| GitHub connection | 1st gen Cloud Build GitHub App | Repo `GraysonLee08/SailLine` connected |
| Build trigger | `sailline-api-deploy` | Branch `^main$`, config `infra/cloudbuild.yaml`, runs as `sailline-cloudbuild` SA |
| Build config | `infra/cloudbuild.yaml` | Build → push → deploy. Injects `CLOUD_SQL_INSTANCE` env var and `DB_PASSWORD` from Secret Manager. Logs to Cloud Logging only. Timeout 1200s. |

### Cloud Run service

| Resource | Identifier | Notes |
|---|---|---|
| Service | `sailline-api` | Region `us-central1`, public HTTPS endpoint, scales 0–10 |
| Live URL | `https://sailline-api-105706282249.us-central1.run.app` | Reachable from public internet |
| Memory / CPU | 512Mi / 1 CPU | Sufficient for v1 |
| Service account | `sailline-api@sailline.iam.gserviceaccount.com` | Runtime identity |
| VPC connector | `sailline-connector` | Egress: `private-ranges-only` |
| Authentication | Allow unauthenticated | API-level auth via Firebase JWT (Step B) |

### Firebase

| Service | Status |
|---|---|
| Firebase project | Linked to GCP `sailline` project |
| Authentication | Enabled (Email/Password + Google sign-in) |
| Hosting | Configured in `frontend/firebase.json` and `.firebaserc` |
| Web app | Registered, config keys captured |

---

## 4. Repository State

GitHub: **[github.com/GraysonLee08/SailLine](https://github.com/GraysonLee08/SailLine)**

### Code files committed and live

```
backend/
├── Dockerfile              ✅ builds successfully, eccodes installed
├── .dockerignore           ✅
├── requirements.txt        ✅ FastAPI + cfgrib + xarray + numpy + asyncpg + Cloud SQL Connector
├── .env.example            ✅
└── app/
    ├── __init__.py         ✅
    ├── main.py             ✅ FastAPI entry, lifespan-managed DB pool, /health + / endpoints
    ├── config.py           ✅ pydantic-settings, env vars + secret aliases
    ├── db.py               ✅ Cloud SQL Connector + asyncpg pool, lazy + non-fatal startup
    └── routers/
        ├── __init__.py     ✅
        └── health.py       ✅ reports cfgrib status

infra/
└── cloudbuild.yaml         ✅ build → push → deploy, with secrets + CLOUD_SQL_INSTANCE wired

frontend/
├── package.json            ✅ Vite + React scaffolding via firebase init
├── vite.config.js          ✅
├── index.html              ✅
├── firebase.json           ✅
├── .firebaserc             ✅
└── src/                    ✅ minimal React app (placeholder)
```

### Documentation files committed

```
README.md                                          ✅
LICENSE                                            ✅ GPL-2.0
.gitignore                                         ✅

docs/
├── prd.md                                         ✅ Product Requirements (v1.1)
├── architecture.md                                ✅ GCP technical architecture
├── repository-structure.md                        ✅ Build week annotations
├── project-status.md                              ✅ this file
└── infrastructure/
    ├── 01-gcp-bootstrap.md                        ✅ Project + APIs
    ├── 02-firebase-setup.md                       ✅ Firebase Auth + Hosting
    ├── 03-vpc-and-cloud-sql.md                    ✅ Networking + DB
    ├── 04-memorystore-and-storage.md              ✅ Redis + GCS
    ├── 05-dockerfile-and-cfgrib.md                ✅ Container validation
    ├── 06-secrets-and-iam.md                      ✅ Cloud Build SA + AR repo
    └── 07-cloudbuild-cicd.md                      ✅ Trigger + first deploy
```

---

## 5. Verified End-to-End

### Container + cfgrib

The `/health` endpoint returns:

```json
{
  "status": "ok",
  "cfgrib_available": true,
  "cfgrib_version": "0.9.14.1"
}
```

This confirms:

- ✅ HTTPS reachable from public internet
- ✅ Container starts and Uvicorn binds to port 8080
- ✅ FastAPI app responds to requests
- ✅ `cfgrib` Python library imports without error
- ✅ `eccodes` C library is installed and accessible to cfgrib
- ✅ Cloud Run service account is properly attached
- ✅ VPC connector is wired up (visible in service config)
- ✅ Build trigger fires on `git push` to `main`
- ✅ Cloud Build runs as the `sailline-cloudbuild` service account
- ✅ Image pushes to Artifact Registry with both `$SHORT_SHA` and `latest` tags
- ✅ Cloud Run deploys the new revision with correct env vars

### Database roundtrip (Step A)

A temporary smoke-test endpoint (`/api/users/me/test`, since removed) returned:

```json
{
  "db_now": "2026-04-28T15:06:38.912566+00:00",
  "postgres": "PostgreSQL 15.17 on x86_64-pc-linux-gnu",
  "postgis": "3.6 USE_GEOS=1 USE_PROJ=1 USE_STATS=1"
}
```

This confirms:

- ✅ FastAPI lifespan successfully initializes the asyncpg pool at startup
- ✅ `DB_PASSWORD` is correctly injected from Secret Manager into the Cloud Run env
- ✅ `CLOUD_SQL_INSTANCE` env var is correctly set on the service
- ✅ The Cloud SQL Python Connector resolves the instance and authenticates
- ✅ Cloud Run egresses through the VPC connector to Cloud SQL's private IP (`10.69.0.3`)
- ✅ The `sailline` app user has CONNECT + USAGE on `sailline_app`
- ✅ PostGIS 3.6 is installed and queryable
- ✅ The full pool → acquire → execute → return pattern works end-to-end

The smoke-test endpoint was removed after verification to avoid exposing Postgres version info on an unauthenticated route.

---

## 6. What's NOT Yet Wired Up

These pieces are infrastructure-ready but the application code hasn't been written yet:

- ✅ ~~FastAPI doesn't yet connect to Cloud SQL~~ — wired up in Step A
- ❌ Firebase JWT verification doesn't run on API requests (no `app/auth.py` implementation) — **Step B**
- ❌ FastAPI doesn't yet talk to Redis (no client setup)
- ❌ Weather worker not built (Cloud Run Job stub doesn't exist)
- ❌ No real product features (all routing, AIS, AI, GPS recording deferred to weeks 2–8)

This is intentional. Week 1 was infrastructure + DB connectivity; Weeks 2+ are application code on top of it.

---

## 7. Next Steps (Priority Order)

### Immediate (close out Week 1)

**Step B — Wire up Firebase JWT verification**

Goal: First protected endpoint, end-to-end auth flow. Same shape as Step A: verify a single piece of the puzzle in isolation, then build features on top.

1. Add `firebase-admin` to `backend/requirements.txt` (uncomment the existing line)
2. Implement `backend/app/auth.py` — `get_current_user` and `require_pro` dependencies
3. Build minimal React login flow (Email/Google sign-in via Firebase SDK)
4. Add a protected endpoint `/api/users/me` that returns the JWT's claims
5. End-to-end test: log in via React → token stored → call protected endpoint → 200 with user info

### Week 2 — Weather pipeline

Goal: Download GFS, parse with cfgrib, cache to Redis, serve via API.

1. Build `workers/weather_ingest.py` as a Cloud Run Job
2. Set up Cloud Scheduler triggers (HRRR hourly, GFS every 6hr)
3. Implement `app/services/grib.py` — wraps cfgrib parsing
4. Implement `app/routers/weather.py` — serves cached wind grids by bounding box
5. Test fixtures: small GRIB2 file checked into `tests/fixtures/`

### Weeks 3–4 — Isochrone routing engine

The hardest piece. Build it as a standalone Python script first (no FastAPI, no DB) with hardcoded test data. When the math is right, wire it into the API.

1. `app/services/polars.py` — load JSON polars + bilinear interpolation
2. `app/services/isochrone.py` — pure routing algorithm
3. `tests/test_isochrone.py` — known scenarios (beat into steady wind should produce tacking route, etc.)
4. Once standalone validated, wire into `app/routers/routing.py`

### Weeks 5–10

Per the architecture doc roadmap:
- W5: Pre-race UI
- W6: In-race routing (SSE)
- W7: AIS + Stripe
- W8: AI advisor + GPS track recording
- W9: Polars + handicap
- W10: Polish + Mac season launch

---

## 8. Operational Notes

### How to deploy from now on

You don't deploy manually anymore. The flow is:

```powershell
# Local machine
git add .
git commit -m "..."
git push origin main
```

Within ~30 seconds, Cloud Build kicks off. Within ~5 minutes, the new revision is live on Cloud Run.

### How to check on the live API

```bash
# Get the URL
gcloud run services describe sailline-api --region=us-central1 --format='value(status.url)'

# Hit it
curl <URL>/health
```

### How to view logs

```bash
gcloud run services logs read sailline-api --region=us-central1 --limit=50
```

Or in the Console: Cloud Run → sailline-api → Logs tab.

### How to view build history

```bash
gcloud builds list --limit=5
```

Or in the Console: Cloud Build → History.

### Costs to expect right now

Roughly **$50/mo idle** with current setup (no users, no traffic). The big drivers:

- Memorystore Redis ($36/mo flat — always-on)
- Cloud SQL minimum (~$10/mo)
- Cloud Run + Cloud Build + Storage + misc (~$5/mo)

Datalastic, Anthropic, and Stripe costs only kick in once you sign up for those services in Weeks 7–8.

### How to halt costs temporarily

If you ever need to pause spending (e.g., taking a break from the project):

```bash
# Stop Cloud SQL
gcloud sql instances patch sailline-db --activation-policy=NEVER

# Delete Memorystore (recreate later if needed — fastest cost cut)
gcloud redis instances delete sailline-cache --region=us-central1
```

Cloud Run scales to zero so it's free when idle. You don't need to touch it.

---

## 9. Risk Register

The risks that mattered going in have mostly been retired. Remaining risks:

| Risk | Severity | Status |
|---|---|---|
| `cfgrib`/`eccodes` install fails on Cloud Run | High | ✅ retired (validated working) |
| GCP IAM/networking misconfiguration | High | ✅ retired (everything passes verification) |
| Cloud Build CI/CD pipeline | Medium | ✅ retired (auto-deploy verified) |
| Cloud Run → Cloud SQL connectivity through VPC | Medium | ✅ retired (live DB roundtrip confirmed) |
| Isochrone engine too slow for 2-min recalc | High | ⏳ unmitigated until Week 3 |
| NOAA GRIB servers slow/unreliable | Medium | ⏳ mitigation: aggressive caching to GCS |
| Datalastic AIS coverage thin mid-lake | Medium | ⏳ documented limitation, in-app warning planned |
| 10-week timeline aggressive for Mac launch | High | ⏳ ongoing — track weekly |

The biggest remaining technical risk is the isochrone routing engine performance. Plan for that in Weeks 3–4: build it standalone, profile early, reduce heading resolution to 10° if needed.

---

## 10. The Bottom Line

You went from a one-line idea ("real-time routing for sailing races") to a production-deployed backend with fully automated CI/CD **and a verified database roundtrip** in roughly a week of evening work. The infrastructure is real, the documentation is comprehensive, and the path forward is clear.

You're now off the infrastructure track and onto the application track. Every commit ships automatically. Time to write the actual product.

**Next coding session:** Step B — wire up Firebase JWT verification. Add `firebase-admin`, implement `app/auth.py`, build the React login flow, and ship the first protected endpoint. Should be a 2–3 hour session and closes out Week 1 entirely.
