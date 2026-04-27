# SailLine

Real-time race routing for sailors. Pre-race planning, in-race tactical guidance, and AI-powered post-race analysis — built for the Great Lakes racing community.

**Status:** 🚧 Pre-launch · Targeting v1 release for the 2026 Chicago-Mac race season

---

## What it does

SailLine combines NOAA wind forecasts, AIS competitor tracking, and boat-specific polar diagrams to recommend optimal race routes — both before the start and continuously during the race. An AI tactical advisor translates routing math into plain-language guidance so non-expert sailors can make smarter decisions on the water.

### Key features

- **Pre-race planning** with NOAA GFS/HRRR wind forecasts
- **Real-time in-race routing** using isochrone algorithm + boat polars
- **AI tactical advisor** powered by Claude — translates routing data into specific, actionable advice
- **AIS competitor tracking** for tactical situational awareness
- **GPS track recording** for future post-race analysis
- **Handicap support** for PHRF, ORC, ORR-EZ, IRC, MORF
- **Hardware tier** (v2) with Raspberry Pi telemetry for instrument-level accuracy

### Boat classes at launch

Beneteau First 36.7 · J/105 · J/109 · J/111 · Farr 40 · Beneteau First 40.7 · Tartan 10 · Generic PHRF/ORC

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React + Vite, MapboxGL, Firebase Auth |
| Backend | FastAPI (Python 3.12), SSE for real-time streams |
| Database | Cloud SQL (PostgreSQL + PostGIS) |
| Cache | Memorystore for Redis |
| Hosting | Cloud Run (API), Firebase Hosting (frontend) |
| Storage | Cloud Storage (GRIB2 weather files) |
| Background jobs | Cloud Run Jobs + Cloud Scheduler |
| Weather data | NOAA GFS + HRRR (parsed via cfgrib) |
| AIS data | Datalastic API |
| AI | Anthropic Claude API |
| Payments | Stripe |
| CI/CD | Cloud Build |

---

## Project structure

```
sailline/
├── backend/
│   ├── Dockerfile
│   ├── app/
│   │   ├── main.py
│   │   ├── routers/         # API endpoints (routing, races, ais, advisor, etc.)
│   │   ├── services/        # Isochrone engine, polars, GRIB parser, Claude
│   │   ├── models/
│   │   └── db.py
│   ├── workers/             # Cloud Run Jobs (weather ingestion)
│   ├── polars/              # JSON polar data by boat class
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/      # Map, DataPanel, RaceSetup, Auth
│   │   ├── hooks/           # useGPS, useRouting, useAIS
│   │   └── lib/
│   └── package.json
├── infra/
│   ├── cloudbuild.yaml
│   └── schema.sql
└── docs/
    ├── prd.md
    └── architecture.md
```

---

## Local development

### Prerequisites

- Python 3.12+
- Node.js 20+
- Docker (for testing the backend container locally)
- `gcloud` CLI authenticated to your GCP project

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Install eccodes locally (required for cfgrib)
# macOS:
brew install eccodes
# Ubuntu/Debian:
sudo apt-get install libeccodes-dev

# Set environment variables (copy from .env.example)
cp .env.example .env
# Edit .env with your local credentials

# Run the API
uvicorn app.main:app --reload --port 8080
```

API docs available at `http://localhost:8080/docs`.

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local
# Add your Firebase + Mapbox keys to .env.local

npm run dev
```

Frontend runs at `http://localhost:5173`.

### Running the backend in Docker

```bash
cd backend
docker build -t sailline-api .
docker run -p 8080:8080 --env-file .env sailline-api
```

---

## Deployment

The repo deploys to GCP via Cloud Build on push to `main`.

```bash
# Manual deploy of the API
gcloud run deploy sailline-api \
  --source ./backend \
  --region us-central1 \
  --vpc-connector sailline-connector

# Manual deploy of the frontend
cd frontend && npm run build
firebase deploy --only hosting
```

Weather worker jobs are triggered by Cloud Scheduler:

- `weather-hrrr` — hourly
- `weather-gfs` — every 6 hours

---

## Environment variables

### Backend

```
GCP_PROJECT_ID
CLOUD_SQL_INSTANCE          # project:region:instance
DB_USER
DB_NAME
REDIS_HOST                  # Memorystore private IP
REDIS_PORT
GCS_BUCKET
DATALASTIC_API_KEY
ANTHROPIC_API_KEY
STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET
FIREBASE_PROJECT_ID
```

### Frontend

```
VITE_API_URL
VITE_MAPBOX_TOKEN
VITE_STRIPE_PUBLIC_KEY
VITE_FIREBASE_CONFIG        # JSON blob
```

Secrets in production are pulled from GCP Secret Manager.

---

## Roadmap

- **v1.0** — Pre-race planning, in-race routing, AIS, AI advisor, GPS recording, desktop UI
- **v1.5** — Probabilistic ensemble routing (GEFS), wave data, tablet/cockpit layout
- **v2.0** — Post-race AI analysis, Pi hardware module, instrument telemetry ingestion
- **v3.0** — ML-learned polars, custom polar uploads, expanded boat classes

---

## Documentation

- [Product Requirements](./docs/prd.md)
- [Technical Architecture](./docs/architecture.md)

---

## License

Copyright © 2026. All rights reserved.

---

## Contact

Issues and feature requests: please open a GitHub issue.
