# VPC + Cloud SQL Setup

Provisions the networking and managed PostgreSQL database that the FastAPI backend connects to. This is the most error-prone step in GCP setup — follow the order exactly.

> **Region lock:** all resources in this doc go in `us-central1`. Mismatched regions silently fail to connect.

---

## Architecture overview

```
Cloud Run (sailline-api)
   │
   │  via Serverless VPC Connector (10.8.0.0/28)
   ▼
Default VPC ────────────────────────────────────┐
   │                                            │
   │  via VPC Peering (Private Services Access) │
   ▼                                            │
Cloud SQL (sailline-db, private IP)             │
   - postgres user (admin, password in Secret Mgr)
   - sailline user (app role)
   - sailline_app database
   - PostGIS extension
```

---

## Prerequisites

- `01-gcp-bootstrap.md` complete (project + billing + APIs enabled)
- `02-firebase-setup.md` complete (Firebase Auth + Hosting working)
- One additional API needs enabling — done in Step 1 below
- All commands below are written for **Cloud Shell (bash)**. Run them in the GCP Console's built-in Cloud Shell terminal.

---

## Step 1 — Enable Service Networking API

`servicenetworking.googleapis.com` was missed in the bootstrap. It's required for Cloud SQL private IP via VPC peering.

```bash
gcloud services enable servicenetworking.googleapis.com
```

Verify:

```bash
gcloud services list --enabled --filter="name:servicenetworking.googleapis.com"
```

---

## Step 2 — Set region defaults

Avoids having to pass `--region=us-central1` to every command:

```bash
gcloud config set compute/region us-central1
gcloud config set compute/zone us-central1-a
gcloud config set run/region us-central1
```

Verify:

```bash
gcloud config list
```

---

## Step 3 — Allocate an IP range for Google managed services

Cloud SQL with private IP lives in a Google-managed network that peers with your VPC. You need to reserve an IP range it can use:

```bash
gcloud compute addresses create google-managed-services-default \
    --global \
    --purpose=VPC_PEERING \
    --prefix-length=16 \
    --network=default
```

> **Cloud Shell / bash:** All multi-line commands in this doc use `\` for line continuation, which is bash syntax. If you'd rather paste single-line commands, just remove the backslashes and join the lines with spaces.

A `/16` is large but standard for this — Google's documentation recommends it.

---

## Step 4 — Create the VPC peering connection

Connects your VPC to the Google-managed services VPC:

```bash
gcloud services vpc-peerings connect \
    --service=servicenetworking.googleapis.com \
    --ranges=google-managed-services-default \
    --network=default
```

This takes 1–2 minutes. Verify when done:

```bash
gcloud services vpc-peerings list --network=default
```

You should see `servicenetworking-googleapis-com` listed.

---

## Step 5 — Create the Serverless VPC Connector

This is what lets Cloud Run reach private IPs inside your VPC:

```bash
gcloud compute networks vpc-access connectors create sailline-connector \
    --region=us-central1 \
    --network=default \
    --range=10.8.0.0/28
```

The `/28` gives 16 IPs — enough for low concurrency. Default machine type and throughput are fine for v1.

This takes 2–3 minutes. Verify:

```bash
gcloud compute networks vpc-access connectors describe sailline-connector --region=us-central1
```

Look for `state: READY`.

---

## Step 6 — Create the Cloud SQL instance

PostgreSQL 15 with both public and private IP. The public IP has no authorized networks, so it's effectively unreachable directly — we just use it for the Cloud SQL Auth Proxy from local machines during setup.

```bash
gcloud sql instances create sailline-db \
    --database-version=POSTGRES_15 \
    --tier=db-f1-micro \
    --region=us-central1 \
    --network=default \
    --enable-google-private-path \
    --storage-size=10GB \
    --storage-auto-increase
```

This takes 5–10 minutes. Be patient.

> **If `db-f1-micro` is rejected as deprecated**, use the Enterprise edition equivalent:
> ```
> --edition=ENTERPRISE --tier=db-custom-1-3840
> ```
> Costs ~$25/mo instead of ~$10/mo, but is the modern path forward.

Verify:

```bash
gcloud sql instances describe sailline-db --format="value(state,ipAddresses)"
```

Should show `RUNNABLE` plus a public IP and a private IP.

---

## Step 7 — Set the postgres user password

A strong password is generated and stored in Secret Manager so it never touches your codebase or shell history.

A strong password is generated and stored in Secret Manager so it never touches your codebase or shell history.

```bash
PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | head -c 24)
gcloud sql users set-password postgres --instance=sailline-db --password="$PASSWORD"
echo -n "$PASSWORD" | gcloud secrets create sailline-db-postgres-password --data-file=-
unset PASSWORD
```

Verify the secret was created:

```bash
gcloud secrets list --filter="name:sailline-db-postgres-password"
```

---

## Step 8 — Create the application database and user

We don't want the app connecting as `postgres`. Create a dedicated app user with a separate password.

**Generate and set the app password:**

```bash
APP_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | head -c 24)
gcloud sql users create sailline --instance=sailline-db --password="$APP_PASSWORD"
echo -n "$APP_PASSWORD" | gcloud secrets create sailline-db-app-password --data-file=-
unset APP_PASSWORD
```

**Create the database:**

```bash
gcloud sql databases create sailline_app --instance=sailline-db
```

Verify:

```bash
gcloud sql databases list --instance=sailline-db
gcloud sql users list --instance=sailline-db
```

You should see the `sailline_app` database and the `sailline` user.

---

## Step 9 — Connect via Cloud Shell to install PostGIS

The easiest way to run admin queries against Cloud SQL is from Cloud Shell — it has `psql` pre-installed and authenticates automatically.

1. Open Cloud Shell from the GCP Console (terminal icon, top right)
2. Run:

```bash
gcloud sql connect sailline-db --user=postgres --database=postgres
```

You'll be prompted for the postgres password. Retrieve it:

```bash
# In a separate Cloud Shell tab:
gcloud secrets versions access latest --secret=sailline-db-postgres-password
```

Paste it when prompted. You'll get a `postgres=>` prompt.

> **First time may fail with a network error.** `gcloud sql connect` temporarily authorizes Cloud Shell's IP. Wait 60 seconds and retry.

---

## Step 10 — Install PostGIS and grant permissions

At the `postgres=>` prompt, switch to the application database and install PostGIS:

```sql
\c sailline_app

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;

-- Verify install
SELECT PostGIS_version();
```

You should see version output like `3.x USE_GEOS=1 USE_PROJ=1 ...`

Grant the app user access:

```sql
GRANT ALL PRIVILEGES ON DATABASE sailline_app TO sailline;
GRANT ALL ON SCHEMA public TO sailline;
GRANT ALL ON ALL TABLES IN SCHEMA public TO sailline;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO sailline;

-- Future tables get permissions automatically
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO sailline;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO sailline;

\q
```

---

## Step 11 — Apply the initial schema

Drop your `infra/schema.sql` content (which we'll populate next) and apply it.

For now, create a minimal `infra/schema.sql` in your repo with just enough to verify connectivity:

```sql
-- infra/schema.sql (initial)
CREATE TABLE IF NOT EXISTS user_profiles (
  id              TEXT PRIMARY KEY,
  tier            TEXT NOT NULL DEFAULT 'free',
  stripe_id       TEXT UNIQUE,
  boat_class      TEXT,
  handicap_system TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

Apply it from Cloud Shell:

```bash
# Upload schema.sql to Cloud Shell (drag-and-drop, or git clone your repo)
gcloud sql connect sailline-db --user=sailline --database=sailline_app < infra/schema.sql
```

Or paste it interactively after running:

```bash
gcloud sql connect sailline-db --user=sailline --database=sailline_app
```

Verify the table exists:

```sql
\dt
-- Should show user_profiles
```

The remaining tables (`race_sessions`, `track_points`, `telemetry_points`) are added in their respective build weeks.

---

## Step 12 — Grant Cloud Run service account access

The Cloud Run service account needs the `Cloud SQL Client` role to connect via the connector. We'll create the service account first; it gets used in the Cloud Build setup later.

```bash
# Create service account for the API
gcloud iam service-accounts create sailline-api \
    --display-name="SailLine API runtime"

# Grant Cloud SQL Client role
gcloud projects add-iam-policy-binding sailline \
    --member="serviceAccount:sailline-api@sailline.iam.gserviceaccount.com" \
    --role="roles/cloudsql.client"

# Grant Secret Manager access (for reading DB passwords)
gcloud projects add-iam-policy-binding sailline \
    --member="serviceAccount:sailline-api@sailline.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"

# Grant Firebase Auth verification (for verifying JWTs)
gcloud projects add-iam-policy-binding sailline \
    --member="serviceAccount:sailline-api@sailline.iam.gserviceaccount.com" \
    --role="roles/firebaseauth.admin"
```

---

## Step 13 — Capture connection info for the backend

You'll need the instance connection name and private IP for the FastAPI backend. Run:

```bash
gcloud sql instances describe sailline-db \
    --format="value(connectionName,ipAddresses[1].ipAddress)"
```

Output looks like:

```
sailline:us-central1:sailline-db    10.x.x.x
```

Save these — they go in your `backend/.env.example` and into Cloud Run env vars later:

```bash
CLOUD_SQL_INSTANCE=sailline:us-central1:sailline-db
DB_HOST=10.x.x.x   # private IP
DB_NAME=sailline_app
DB_USER=sailline
# DB_PASSWORD comes from Secret Manager at runtime
```

---

## Verification checklist

Run all of these — every line should succeed:

```bash
# VPC peering exists
gcloud services vpc-peerings list --network=default

# VPC connector is READY
gcloud compute networks vpc-access connectors describe sailline-connector --region=us-central1 --format="value(state)"

# Cloud SQL is RUNNABLE
gcloud sql instances describe sailline-db --format="value(state)"

# Database exists
gcloud sql databases list --instance=sailline-db --format="value(name)" | grep sailline_app

# App user exists
gcloud sql users list --instance=sailline-db --format="value(name)" | grep sailline

# Both secrets exist
gcloud secrets list --filter="name~sailline-db" --format="value(name)"

# Service account has roles
gcloud projects get-iam-policy sailline --flatten="bindings[].members" --format="value(bindings.role)" --filter="bindings.members:sailline-api@sailline.iam.gserviceaccount.com"
```

---

## Troubleshooting

**`Step 4 fails with "API not enabled"`**
You missed Step 1. Enable `servicenetworking.googleapis.com` and retry.

**`Step 5 fails with "Range overlaps"`**
Your VPC subnets already use `10.8.0.0/28`. Pick a different `/28` like `10.9.0.0/28` and retry.

**`Step 6 instance creation hangs forever`**
Cloud SQL provisioning genuinely takes 5–10 minutes. If it's been more than 15 minutes, check `gcloud sql operations list --instance=sailline-db` for errors.

**`Step 9 — gcloud sql connect fails with timeout`**
Cloud Shell's IP needs to be authorized. The first attempt does this automatically but takes ~60 seconds to propagate. Wait and retry. If it still fails, manually add Cloud Shell's IP:
```bash
gcloud sql instances patch sailline-db --authorized-networks=$(curl -s ifconfig.me)
```

**`PostGIS extension fails to install`**
Make sure you `\c sailline_app` first to switch into the application database. PostGIS is per-database, not cluster-wide.

**`Permission denied for schema public` after switching to sailline user**
You forgot the `GRANT ALL ON SCHEMA public TO sailline` step. Reconnect as postgres and run it.

---

## What's next

With networking and the database in place, the next infrastructure docs are:

- `03-memorystore-and-storage.md` — Redis cache + Cloud Storage bucket for GRIB files
- `04-secrets-and-iam.md` — remaining Secret Manager entries (Datalastic, Anthropic, Stripe keys)
- `05-cloudbuild-cicd.md` — Artifact Registry repo + Cloud Build pipeline

After those, you have everything needed to deploy the Week 1 hello-world FastAPI app and confirm it can read from Cloud SQL.
