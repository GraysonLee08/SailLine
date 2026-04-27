# SailLine — Technical Architecture (GCP)

**Version:** 2.0
**Target:** v1 launch before Chicago-Mac race season (July 2026)
**Developer:** Solo · Python primary · React comfortable
**Cloud:** Google Cloud Platform (single provider)

---

## 1. Stack Decisions

Single-provider architecture on GCP. Trades slightly higher minimum cost (~$50/mo idle) for unified IAM, integrated logging, private VPC networking, and one console for everything.

| Layer | GCP Service | Why |
|---|---|---|
| **Frontend hosting** | Firebase Hosting | Free tier, global CDN, integrates with Firebase Auth |
| **Auth** | Firebase Auth | Built-in JWT, free up to 50K MAU, drop-in React SDK |
| **Backend** | Cloud Run | Containerized FastAPI, scales to zero, supports SSE + custom system libs (`eccodes`) |
| **Database** | Cloud SQL for PostgreSQL | Managed Postgres, PostGIS extension available, private VPC connection |
| **Cache** | Memorystore for Redis (Basic 1GB) | Managed, low-latency, in-VPC; minimum ~$35/mo |
| **File storage** | Cloud Storage | GRIB2 file caching, lifecycle rules for auto-cleanup |
| **Background jobs** | Cloud Run Jobs + Cloud Scheduler | Cron-style weather ingestion |
| **Container registry** | Artifact Registry | Built-in, private, integrated with Cloud Run |
| **Secrets** | Secret Manager | API keys, JWT secrets, DB credentials |
| **CI/CD** | Cloud Build (or GitHub Actions) | Deploy on git push to main |
| **Monitoring** | Cloud Logging + Cloud Monitoring | Structured logs, alerts, dashboards |
| **Networking** | VPC + Serverless VPC Connector | Private connection from Cloud Run to Cloud SQL/Memorystore |

### External (non-GCP) dependencies

| Service | Why not on GCP |
|---|---|
| Datalastic AIS | Third-party data provider; no GCP equivalent |
| Anthropic Claude API | Third-party AI provider |
| Stripe | Payments processor |
| NOAA GFS/HRRR | Public data source |
| MapboxGL | Maps SDK (could swap to Google Maps JS API but Mapbox has stronger sailing/marine support) |

---

## 2. Why Cloud Run over App Engine Standard

Cloud Run is the right choice here for two non-negotiable reasons:

1. **SSE streams require longer request lifetimes.** App Engine Standard caps requests at 60 seconds. Cloud Run supports up to 60 minutes per request. Even better, when an SSE connection drops, the browser's `EventSource` reconnects automatically — so a 4-hour race session works fine with Cloud Run reconnecting once per hour.

2. **`cfgrib` requires the `eccodes` C library.** App Engine Standard runs in a sandboxed runtime with no system package installation. Cloud Run lets you install anything you want in your Dockerfile.

Cloud Run also scales to zero, so during the months of pre-launch development you pay $0 for the API itself. Cloud SQL and Memorystore are the floor on idle cost.

---

## 3. Network Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Browser (user)                                              │
│  └─ React SPA + Firebase Auth SDK                           │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTPS
        ┌──────────────┴──────────────┐
        ▼                             ▼
┌──────────────────┐         ┌──────────────────┐
│ Firebase         │         │ Cloud Run        │
│ Hosting          │         │ sailline-api     │
│ (static React)   │         │ (FastAPI)        │
└──────────────────┘         └────────┬─────────┘
                                      │
                                      ▼ Serverless VPC Connector
                  ┌───────────────────┴───────────────────┐
                  │              VPC (private)             │
                  │  ┌─────────────┐    ┌──────────────┐  │
                  │  │ Cloud SQL   │    │ Memorystore  │  │
                  │  │ Postgres    │    │ Redis        │  │
                  │  │ + PostGIS   │    │              │  │
                  │  └─────────────┘    └──────────────┘  │
                  └────────────────────────────────────────┘
                                      │
                                      ▼ Public egress
                          ┌─────────────────────┐
                          │ External APIs       │
                          │  - Datalastic       │
                          │  - Anthropic Claude │
                          │  - Stripe           │
                          │  - NOAA (worker)    │
                          └─────────────────────┘

Cloud Run Jobs (Cloud Scheduler triggers):
  - weather-ingest-hrrr (hourly)
  - weather-ingest-gfs (every 6 hours)
  Both write parsed wind data to Memorystore + Cloud Storage
```

---

## 4. Repository Structure

```
sailline/
├── backend/
│   ├── Dockerfile               # Includes apt-get install libeccodes-dev
│   ├── app/                     # FastAPI app (same structure as before)
│   ├── workers/
│   │   └── weather_ingest.py    # Cloud Run Job entry point
│   └── requirements.txt
├── frontend/
│   ├── src/                     # React app
│   ├── firebase.json            # Firebase Hosting config
│   └── package.json
├── infra/
│   ├── cloudbuild.yaml          # Build + deploy pipeline
│   ├── terraform/               # Optional: IaC for GCP resources
│   └── schema.sql               # Cloud SQL initial schema
└── README.md
```

---

## 5. Backend Container

```dockerfile
# backend/Dockerfile
FROM python:3.12-slim

# eccodes is required for cfgrib to parse NOAA GRIB2 files
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Cloud Run injects PORT env var (default 8080)
ENV PORT=8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
```

Single worker per container, let Cloud Run scale concurrency horizontally. SSE connections are long-lived; multiple workers per container creates GIL contention.

---

## 6. Authentication Flow

Firebase Auth issues JWTs to the React client. The FastAPI backend verifies them using Firebase's public keys.

```python
# app/auth.py
from firebase_admin import auth as fb_auth, initialize_app

initialize_app()  # uses GOOGLE_APPLICATION_CREDENTIALS automatically on Cloud Run

async def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        decoded = fb_auth.verify_id_token(token)
        user_id = decoded["uid"]
    except Exception:
        raise HTTPException(401, "Invalid token")

    tier = await get_user_tier(user_id)  # query Cloud SQL
    return {"user_id": user_id, "tier": tier}

def require_pro(user=Depends(get_current_user)):
    if user["tier"] not in ("pro", "hardware"):
        raise HTTPException(403, "Pro subscription required")
```

Frontend uses the Firebase JS SDK to handle login flows (email + Google sign-in are the easiest to enable).

---

## 7. Database Connection

Cloud SQL connects via the Cloud SQL Auth Proxy (private IP, in-VPC). No exposing Postgres to the internet.

```python
# app/db.py
import asyncpg
from google.cloud.sql.connector import Connector, IPTypes

connector = Connector()

async def get_pool():
    async def getconn():
        return await connector.connect_async(
            instance_connection_name=os.environ["CLOUD_SQL_INSTANCE"],  # project:region:instance
            driver="asyncpg",
            user=os.environ["DB_USER"],
            password=await get_secret("db-password"),  # Secret Manager
            db=os.environ["DB_NAME"],
            ip_type=IPTypes.PRIVATE,
        )
    return await asyncpg.create_pool(connect=getconn, min_size=1, max_size=10)
```

---

## 8. Weather Worker as Cloud Run Job

```python
# workers/weather_ingest.py — entry point
import asyncio, sys

async def main():
    source = sys.argv[1]  # "hrrr" or "gfs"
    grib_path = await download_latest_cycle(source)
    wind_data = parse_grib_to_wind_grid(grib_path)

    # Cache to Memorystore Redis
    await redis_client.setex(f"weather:{source}:latest",
                             3600 if source == "hrrr" else 21600,
                             wind_data.to_json())

    # Backup to Cloud Storage for durability
    await gcs_bucket.blob(f"{source}/latest.json").upload_from_string(wind_data.to_json())

if __name__ == "__main__":
    asyncio.run(main())
```

Triggered by Cloud Scheduler:
- HRRR job: cron `0 * * * *` (hourly)
- GFS job: cron `5 */6 * * *` (every 6 hours)

Cloud Run Jobs use the same container image as the API (just different entry point).

---

## 9. Database Schema

(Identical to v1 — runs in Cloud SQL instead of Supabase. PostGIS extension installed via `CREATE EXTENSION postgis;` after instance creation.)

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE user_profiles (
  id              TEXT PRIMARY KEY,  -- Firebase Auth UID
  tier            TEXT NOT NULL DEFAULT 'free',
  stripe_id       TEXT UNIQUE,
  boat_class      TEXT,
  handicap_system TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE race_sessions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     TEXT REFERENCES user_profiles,
  mode        TEXT NOT NULL,
  boat_class  TEXT NOT NULL,
  marks       JSONB NOT NULL,
  started_at  TIMESTAMPTZ,
  ended_at    TIMESTAMPTZ,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE track_points (
  id          BIGSERIAL PRIMARY KEY,
  session_id  UUID REFERENCES race_sessions,
  recorded_at TIMESTAMPTZ NOT NULL,
  position    GEOGRAPHY(POINT, 4326) NOT NULL,
  speed_kts   FLOAT,
  heading_deg FLOAT,
  wind_speed  FLOAT,
  wind_dir    FLOAT,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX track_session_time_idx ON track_points(session_id, recorded_at);

CREATE TABLE telemetry_points (
  id                  BIGSERIAL PRIMARY KEY,
  session_id          UUID REFERENCES race_sessions,
  recorded_at         TIMESTAMPTZ NOT NULL,
  position            GEOGRAPHY(POINT, 4326),
  boat_speed_kts      FLOAT,
  true_wind_speed     FLOAT,
  true_wind_dir       FLOAT,
  apparent_wind_speed FLOAT,
  apparent_wind_dir   FLOAT,
  heading_mag         FLOAT,
  heel_angle          FLOAT,
  created_at          TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 10. CI/CD with Cloud Build

```yaml
# infra/cloudbuild.yaml
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', 'us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:$SHORT_SHA',
           './backend']
  - name: 'gcr.io/cloud-builders/docker'
    args: ['push', 'us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:$SHORT_SHA']
  - name: 'gcr.io/google.com/cloudsdktool/cloud-sdk'
    entrypoint: gcloud
    args: ['run', 'deploy', 'sailline-api',
           '--image', 'us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:$SHORT_SHA',
           '--region', 'us-central1',
           '--vpc-connector', 'sailline-connector',
           '--service-account', 'sailline-api@$PROJECT_ID.iam.gserviceaccount.com']
```

Trigger on push to `main`. Frontend deployed separately via `firebase deploy --only hosting` (or Cloud Build step for it).

---

## 11. Cost Model on GCP

| Service | Idle | At 100 Pro users |
|---|---|---|
| Cloud Run (API) | ~$0 (scales to zero) | ~$15/mo |
| Cloud Run Jobs (workers) | ~$1/mo | ~$1/mo |
| Cloud SQL (db-f1-micro shared) | ~$10/mo | ~$25/mo (move to db-g1-small) |
| Memorystore Redis (Basic 1GB) | ~$35/mo | ~$35/mo |
| Cloud Storage | ~$1/mo | ~$3/mo |
| Firebase Hosting | $0 (free tier) | $0–5/mo |
| Firebase Auth | $0 (free tier) | $0 |
| Cloud Scheduler | $0 (free tier) | $0 |
| Secret Manager | <$1/mo | <$1/mo |
| Egress | ~$1/mo | ~$5/mo |
| **GCP subtotal** | **~$50/mo** | **~$90/mo** |
| Datalastic AIS | ~$220/mo | ~$330/mo |
| Anthropic Claude | ~$5/mo | ~$30/mo |
| Stripe fees | $0 | ~$73/mo |
| **All-in total** | **~$275/mo** | **~$525/mo** |

### Implications for break-even

| Subscribers | Revenue | All-in cost | Margin |
|---|---|---|---|
| 22 Pro | $330 | ~$280 | ~$50/mo |
| 50 Pro | $750 | ~$370 | +$380/mo |
| 100 Pro | $1,500 | ~$525 | +$975/mo |
| 500 Pro | $7,500 | ~$1,200 | +$6,300/mo |

Break-even moves from 22 subscribers to roughly 22–25, basically unchanged. The slightly higher GCP idle cost is offset over time by Memorystore's better latency vs. Upstash and avoiding the multi-vendor management tax.

---

## 12. Build Order — 10-Week Plan (GCP)

The first week now includes GCP project setup. Allocate 2–3 days to learning + setup before any application code.

| Week | Focus | Done When |
|---|---|---|
| **1** | GCP setup | Project, VPC, Cloud SQL instance, Memorystore, Artifact Registry, Firebase project all provisioned. Hello-world Cloud Run service deployed and reaching Cloud SQL via VPC connector. |
| **2** | Weather pipeline | Container builds with `eccodes`, GRIB2 parsed, data cached to Memorystore + GCS, served via API |
| **3–4** | Routing engine | Isochrone algorithm + polar interpolation working in Python (no UI yet) |
| **5** | Pre-race UI | Map, mark placement, route overlay rendered in React |
| **6** | In-race routing | SSE stream, GPS hook, recalculation cadence working end-to-end |
| **7** | AIS + Stripe | Datalastic integration, competitor map layer, subscription tier gating live |
| **8** | AI + GPS recording | Claude tactical advisor wired up; track points written to Cloud SQL |
| **9** | Polars + handicap | All 8 boat classes loaded; PHRF/ORC corrected time displayed |
| **10** | Polish + launch | Errors handled, monitoring dashboards, Mac course pre-loaded, MORF soft launch |

### Deferred from v1 (unchanged)

| Feature | Target |
|---|---|
| Probabilistic ensemble routing (GEFS) | v1.5 |
| Post-race analysis UI | v2 |
| Wave Watch III | v1.5 |
| Tablet/cockpit layout | v1.5 |
| Pi telemetry (public) | v2 |

---

## 13. Technical Risks (GCP-specific updates)

| Risk | Severity | Mitigation |
|---|---|---|
| `eccodes` install in container fails | Medium | Test Dockerfile build locally Week 1 before deploying |
| VPC connector misconfig blocks DB access | Medium | Standard GCP gotcha; resolve in Week 1 setup; well-documented |
| Cloud Run cold start latency (~1-3s) | Low | Set `min-instances: 1` on the API service for production (~$15/mo) to keep one warm |
| Memorystore minimum cost ($35/mo idle) | Low | Acceptable trade for latency + private networking; can swap to Upstash if pre-revenue runway is tight |
| Cloud SQL connection pooling | Medium | Use Cloud SQL Connector library (handles auth, IAM, pooling) — don't roll your own |
| Isochrone engine too slow for 2-min recalc | High | Profile early; reduce heading resolution; consider Cloud Run CPU boost |

---

## 14. One Multi-Vendor Exception Worth Considering

If pre-launch budget is tight, you can swap **Memorystore → Upstash Redis** to save ~$35/mo while you have zero users. Upstash has a generous free tier and pay-per-request pricing. Once you have 50+ Pro subscribers, migrate to Memorystore for the in-VPC latency benefit.

This is the only place where the multi-vendor approach has a real cost advantage. Everything else on GCP is genuinely cheaper or equivalent.

---

## 15. Hardware Telemetry Endpoint (v2 — Built Now, Not Exposed)

Unchanged from v1 plan. Endpoint exists in the FastAPI app, gated by `require_hardware_tier`, hidden from API docs via `include_in_schema=False`. The Pi module (Signal K server) will POST NMEA-parsed telemetry every 5 seconds once v2 ships. Database table created in v1 schema migration so no schema changes are needed at v2 launch.
