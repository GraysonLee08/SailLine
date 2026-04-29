# Memorystore Redis + Cloud Storage Setup

Provisions the Redis cache (for AIS data and parsed weather forecasts) and the Cloud Storage bucket (for GRIB2 weather file backups). Both connect to the same VPC as Cloud SQL, sharing the Serverless VPC Connector.

> All commands assume **Cloud Shell (bash)** in the GCP Console. Region: `us-central1`.

---

## What you're building

```
Cloud Run (sailline-api)
   │
   │  via Serverless VPC Connector
   ▼
Default VPC ──────────────────────────────────┐
   │                                          │
   │  ┌─ Cloud SQL (already exists)           │
   ├─ Memorystore Redis (10.x.x.x:6379)       │
   │                                          │
   └──────────────────────────────────────────┘
   │
   ▼ (no VPC required)
Cloud Storage bucket: sailline-weather-grib
```

Memorystore lives in the VPC like Cloud SQL. Cloud Storage is a global object store accessed via API — no VPC connector needed.

---

## Prerequisites

- `03-vpc-and-cloud-sql.md` complete (VPC, peering, connector, Cloud SQL all working)
- `gcloud` authenticated in Cloud Shell

---

## Part A: Memorystore Redis

### Step 1 — Enable the Redis API

```bash
gcloud services enable redis.googleapis.com
```

Verify:

```bash
gcloud services list --enabled --filter="name:redis.googleapis.com"
```

### Step 2 — Create the Redis instance

Use the **Basic tier** (no replication) at the smallest size (1 GB). Basic tier has no SLA but is dramatically cheaper than Standard. We can upgrade later if needed.

```bash
gcloud redis instances create sailline-cache \
    --size=1 \
    --region=us-central1 \
    --tier=basic \
    --redis-version=redis_7_2 \
    --network=default \
    --connect-mode=PRIVATE_SERVICE_ACCESS
```

Takes 3–5 minutes. The `PRIVATE_SERVICE_ACCESS` mode reuses the VPC peering you already set up for Cloud SQL — no new networking required.

> **If `redis_7_2` is rejected**, try `redis_7_0` or `redis_6_x`. Available versions vary by region.

### Step 3 — Get the Redis host and port

```bash
gcloud redis instances describe sailline-cache \
    --region=us-central1 \
    --format='value(host,port)'
```

Output looks like:

```
10.69.0.4    6379
```

Save these for `backend/.env.example`:

```
REDIS_HOST=10.69.0.4
REDIS_PORT=6379
```

> Memorystore Basic tier doesn't require auth by default. The VPC isolation is the security boundary. Don't expose this instance publicly.

### Step 4 — Verify

```bash
gcloud redis instances describe sailline-cache --region=us-central1 --format='value(state)'
```

Should output `READY`.

You can't easily test the Redis connection from Cloud Shell because Memorystore is private-IP-only. The first real test will happen when Cloud Run connects to it via the VPC connector.

---

## Part B: Cloud Storage Bucket

This bucket holds:
- GRIB2 weather file backups (used as fallback when Memorystore eviction happens)
- Long-term wind data archive (useful for v2 post-race analysis)

### Step 5 — Create the bucket

Bucket names are globally unique across all of GCP. The convention used here is `<project-id>-<purpose>`:

```bash
gcloud storage buckets create gs://sailline-weather \
    --project=sailline \
    --location=us-central1 \
    --uniform-bucket-level-access \
    --public-access-prevention
```

Flags explained:
- `--uniform-bucket-level-access` — IAM-only permissions (no per-object ACLs). Modern best practice.
- `--public-access-prevention` — explicitly blocks any future attempt to make the bucket public.

### Step 6 — Set lifecycle rules (auto-cleanup of old GRIB files)

GRIB files are large (~50 MB each). Without cleanup, the bucket grows indefinitely. Set a lifecycle rule that deletes objects older than 30 days:

```bash
cat > /tmp/lifecycle.json << 'EOF'
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {"age": 30}
      }
    ]
  }
}
EOF

gcloud storage buckets update gs://sailline-weather --lifecycle-file=/tmp/lifecycle.json
rm /tmp/lifecycle.json
```

Verify the rule was applied:

```bash
gcloud storage buckets describe gs://sailline-weather --format='value(lifecycle)'
```

You should see the delete-after-30-days rule.

### Step 7 — Grant the API service account access to the bucket

The `sailline-api` service account needs to read and write to this bucket. Grant `Storage Object Admin` scoped to just this bucket:

```bash
gcloud storage buckets add-iam-policy-binding gs://sailline-weather \
    --member="serviceAccount:sailline-api@sailline.iam.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"
```

> Bucket-scoped IAM is more secure than project-wide. The API can only touch this one bucket, not any others you create later.

### Step 8 — Verify the bucket

```bash
# List buckets in your project
gcloud storage ls

# Test write access (creates a tiny test file, then deletes it)
echo "test" | gcloud storage cp - gs://sailline-weather/test.txt
gcloud storage ls gs://sailline-weather/
gcloud storage rm gs://sailline-weather/test.txt
```

If `gcloud storage ls gs://sailline-weather/` shows your test file, everything's working.

---

## Verification checklist

```bash
# Redis instance is READY
gcloud redis instances describe sailline-cache --region=us-central1 --format='value(state)'

# Redis is using PRIVATE_SERVICE_ACCESS
gcloud redis instances describe sailline-cache --region=us-central1 --format='value(connectMode)'

# Bucket exists and has uniform access
gcloud storage buckets describe gs://sailline-weather --format='value(name,iamConfiguration.uniformBucketLevelAccess.enabled)'

# Lifecycle rule is set
gcloud storage buckets describe gs://sailline-weather --format='value(lifecycle.rule[0].condition.age)'

# Service account has bucket access
gcloud storage buckets get-iam-policy gs://sailline-weather --format='value(bindings)' | grep sailline-api
```

Expected:
```
READY
PRIVATE_SERVICE_ACCESS
sailline-weather    True
30
serviceAccount:sailline-api@sailline.iam.gserviceaccount.com
```

---

## Capture for backend env vars

Add these lines to your `backend/.env.example`:

```
REDIS_HOST=10.69.0.4              # actual private IP from Step 3
REDIS_PORT=6379
GCS_WEATHER_BUCKET=sailline-weather
```

The values become Cloud Run environment variables at deploy time.

---

## Cost notes

| Service | Cost |
|---|---|
| Memorystore Redis Basic 1GB | ~$36/month flat (always-on) |
| Cloud Storage Standard | ~$0.02/GB/month + egress |

Memorystore is the bigger ongoing cost. If pre-launch budget is tight, the alternative is to use Upstash Redis instead (free tier, pay-per-request) and skip this entire Part A. The trade is multi-vendor management vs. ~$36/month savings.

For Cloud Storage, expect <$1/month at the volumes SailLine will produce (a handful of 50MB GRIB files per day, deleted after 30 days = roughly 1.5GB ongoing).

---

## Troubleshooting

**Step 2 fails: "Operation type DELETE_PEER_NETWORK_CONNECTION already in progress"**
Some other operation is touching the VPC peering. Wait 60 seconds and retry.

**Step 2 fails: "no available IP ranges"**
The Google managed services range you allocated in Step 3 of `03-vpc-and-cloud-sql.md` is too small or already exhausted. Should not happen with a `/16`. If it does, allocate a second range and retry.

**Step 5 fails: "bucket name already exists"**
Bucket names are globally unique. Pick a different name like `gs://sailline-grayson-weather`. Update the rest of the doc accordingly.

**Step 8 — `gcloud storage cp` fails with permission denied**
Your user account doesn't have `storage.objects.create` on the bucket. Add yourself temporarily for testing:
```bash
gcloud storage buckets add-iam-policy-binding gs://sailline-weather \
    --member="user:your-email@example.com" \
    --role="roles/storage.objectAdmin"
```

---

## What's next

- `05-secrets-and-iam.md` — store API keys for Datalastic, Anthropic, Stripe; create the Cloud Build service account
- `06-cloudbuild-cicd.md` — Artifact Registry repo + Cloud Build pipeline → Cloud Run

After those, infrastructure is complete and you can deploy the Week 1 hello-world FastAPI container.
