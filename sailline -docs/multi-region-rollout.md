# Multi-Region Rollout

Code is region-aware. Production currently has Cloud Run Jobs + Scheduler triggers for great_lakes only. This runbook provisions the other 9 regions.

**Time to complete:** ~20 minutes. Most of it is waiting for `gcloud run jobs create` to finish.

---

## Pre-flight

Code merged to `main` and Cloud Build deployed the new `sailline-api` image. Verify:

```bash
# API is on the new code
curl -I "https://$(gcloud run services describe sailline-api --region=us-central1 --format='value(status.url)' | sed 's|https://||')/api/weather?region=chesapeake&source=hrrr"
# Expect: 503 (region known, no data yet) — NOT 404 (which would mean the new region registry didn't deploy)
```

If you get 404, the API is still on the old code. Wait for Cloud Build to finish.

---

## Step 1 — Update the existing great_lakes jobs

The existing `sailline-ingest-gfs` and `sailline-ingest-hrrr` jobs run the worker without `--region`, which now defaults to `great_lakes`. They'll keep working as-is — but they're now writing to `weather:{source}:great_lakes:latest` instead of the legacy `weather:{source}:latest`.

Trigger one manual run of each so the new keys populate immediately. Otherwise users in great_lakes will hit the legacy fallback for up to 6 hours (until the next GFS cycle).

```bash
gcloud run jobs execute sailline-ingest-hrrr --region=us-central1 --wait
gcloud run jobs execute sailline-ingest-gfs --region=us-central1 --wait
```

Verify the new keys exist (from a Cloud Shell with VPC access, or via a redis-cli pod):

```
GET weather:hrrr:great_lakes:latest    # should return data
GET weather:gfs:great_lakes:latest     # should return data
```

Optional rename for consistency (purely cosmetic; not required):

```bash
# If you want job names to match the new pattern, you'd delete and recreate
# them as sailline-ingest-hrrr-great-lakes / sailline-ingest-gfs-great-lakes.
# Skip unless you care — the existing names still work.
```

---

## Step 2 — Create jobs for the other 9 regions

Pattern: one job per (source, region) pair. The full matrix is 19 jobs (10 regions × 2 sources, minus 1 because hawaii is GFS-only).

Run this loop in Cloud Shell (or your local shell with `gcloud` auth):

```bash
PROJECT_ID=$(gcloud config get-value project)
IMAGE="us-central1-docker.pkg.dev/${PROJECT_ID}/sailline/api:latest"

# (source, region) pairs — must mirror app/regions.py
PAIRS=(
  "hrrr chesapeake"
  "gfs  chesapeake"
  "hrrr long_island_sound"
  "gfs  long_island_sound"
  "hrrr new_england"
  "gfs  new_england"
  "hrrr florida"
  "gfs  florida"
  "hrrr gulf_coast"
  "gfs  gulf_coast"
  "hrrr socal"
  "gfs  socal"
  "hrrr sf_bay"
  "gfs  sf_bay"
  "hrrr pnw"
  "gfs  pnw"
  "gfs  hawaii"
  # NOTE: no "hrrr hawaii" — HRRR is CONUS-only.
)

for pair in "${PAIRS[@]}"; do
  read -r src region <<< "$pair"
  region_dash=${region//_/-}
  job_name="sailline-ingest-${src}-${region_dash}"

  echo "Creating ${job_name}..."

  gcloud run jobs create "${job_name}" \
    --image="${IMAGE}" \
    --region=us-central1 \
    --service-account="sailline-api@${PROJECT_ID}.iam.gserviceaccount.com" \
    --vpc-connector=sailline-connector \
    --vpc-egress=private-ranges-only \
    --task-timeout=10m \
    --memory=1Gi \
    --cpu=1 \
    --max-retries=1 \
    --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},DB_NAME=sailline_app,DB_USER=sailline,DB_HOST=10.69.0.3,REDIS_HOST=10.69.0.4,REDIS_PORT=6379,GCS_WEATHER_BUCKET=sailline-weather" \
    --command="python" \
    --args="-m,workers.weather_ingest,${src},--region,${region}"
done
```

> Replace `10.69.0.3` and `10.69.0.4` with your actual Cloud SQL and Memorystore private IPs (same ones in `infra/cloudbuild.yaml`).

This takes ~10–15 minutes — each job creation is a separate API call.

---

## Step 3 — Trigger each new job once to populate data

Otherwise users hitting these regions will get 503 until the first scheduled run.

```bash
for pair in "${PAIRS[@]}"; do
  read -r src region <<< "$pair"
  region_dash=${region//_/-}
  job_name="sailline-ingest-${src}-${region_dash}"
  echo "Running ${job_name}..."
  gcloud run jobs execute "${job_name}" --region=us-central1 --wait &
done
wait
```

The `&` + `wait` runs them in parallel — saves ~30 minutes vs. sequential. NOAA can handle the load (we're hitting different byte ranges of files they're already serving).

Spot-check one or two:

```bash
curl -I "https://sailline-api-...us.a.run.app/api/weather?region=sf_bay&source=hrrr"
# Expect: 200, content-encoding: gzip, etag: "..."
```

---

## Step 4 — Schedule each job

HRRR runs hourly (5 minutes past the hour, accounting for ~2h publish lag). GFS runs every 6 hours (30 minutes past 05/11/17/23 UTC, accounting for ~5h publish lag). Same cadence as the existing great_lakes triggers.

```bash
SCHEDULER_SA="sailline-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"

for pair in "${PAIRS[@]}"; do
  read -r src region <<< "$pair"
  region_dash=${region//_/-}
  job_name="sailline-ingest-${src}-${region_dash}"
  trigger_name="sailline-trigger-${src}-${region_dash}"

  if [ "$src" = "hrrr" ]; then
    schedule="5 * * * *"
  else
    schedule="30 5,11,17,23 * * *"
  fi

  echo "Creating ${trigger_name} (${schedule})..."

  gcloud scheduler jobs create http "${trigger_name}" \
    --location=us-central1 \
    --schedule="${schedule}" \
    --time-zone=Etc/UTC \
    --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job_name}:run" \
    --http-method=POST \
    --oauth-service-account-email="${SCHEDULER_SA}"
done
```

Verify:

```bash
gcloud scheduler jobs list --location=us-central1 --filter="name:sailline-trigger" --format="table(name,schedule,state)"
```

You should see 19 triggers total (the original 2 great_lakes + 17 new ones if you renamed great_lakes, or 18 new ones if you didn't).

---

## Step 5 — Remove the legacy fallback

After the first scheduled cycle has run successfully (give it 1 hour for HRRR, 6 hours for GFS), the great_lakes legacy fallback in `app/routers/weather.py` and `app/routers/weather.py:_read_latest_gcs` can be deleted.

Search for `LEGACY_REGION` in `backend/app/routers/weather.py` — that's the marker. Drop:

- The `LEGACY_REGION = "great_lakes"` constant
- The fallback block reading `weather:{source}:latest`
- The `region == LEGACY_REGION` branch in `_read_latest_gcs`
- The `skip_subdirs` parameter on `_latest_blob_under` (only used by the fallback)

Followed by a corresponding test cleanup in `backend/tests/test_weather_router.py` — delete `test_great_lakes_legacy_redis_fallback`. The other tests stand.

Push to `main`. Cloud Build deploys. Done.

---

## Cost impact

Going from 2 jobs → 19 jobs:

| Item | Before | After | Notes |
|---|---|---|---|
| Cloud Run Jobs runtime | ~$5/mo | ~$40/mo | Each job runs ~30s × 720 invocations (HRRR) or 120 (GFS) |
| GCS storage | ~$0.10/mo | ~$0.30/mo | More objects but each is tiny (~50 KB gzipped) |
| NOAA bandwidth | $0 | $0 | Free public source |
| Memorystore | ~$36/mo | ~$36/mo | Still well under 1 GB total across all keys |

**Total delta: ~$35–40/mo.** If that becomes a problem, the levers (in order of impact):

1. Drop GFS for low-traffic regions — HRRR alone is fine for in-CONUS ones (~50% saving on jobs runtime).
2. Reduce HRRR cadence in low-traffic regions to every 3 hours instead of hourly.
3. Move sf_bay/socal to lower priority and only ingest when the API has hit them recently (lazy ingest — significant code change).

---

## Troubleshooting

**`gcloud run jobs create` fails with `image not found`**
The Cloud Build deploy hasn't completed. Check `gcloud builds list --limit=1`. Wait for SUCCESS.

**Job runs but `/api/weather?region=X` returns 503**
- Check the job's last execution: `gcloud run jobs executions list --job=sailline-ingest-${src}-${region} --region=us-central1`
- If it failed, check logs: `gcloud run jobs executions logs read <execution-id> --region=us-central1`
- Common cause: region's bbox is outside the source's coverage. HRRR is CONUS-only — verify the bbox in `app/regions.py` is inside roughly (21°N–53°N, -135°W–-60°W).

**Scheduler trigger fires but nothing happens**
- Verify the `sailline-scheduler` service account has `roles/run.invoker` on the new job: `gcloud run jobs add-iam-policy-binding ${job_name} --region=us-central1 --member="serviceAccount:${SCHEDULER_SA}" --role="roles/run.invoker"`. New jobs created via this runbook inherit the role from the project but it's worth double-checking on the first one.

**Worker fails with "bbox produced empty grid"**
The region's bbox doesn't intersect the source's data extent. For a US-region GFS run this should never happen (GFS is global). For HRRR, this means the bbox is fully outside CONUS — fix the registry entry.

**Hawaii GFS payload looks tiny / suspicious**
Hawaii's bbox (18.5°N–22.5°N) crosses GFS's 0.25° grid roughly 16×26 cells = 416 points. Expected gzipped size ~5–8 KB. If much smaller, check that GFS's lon convention in the response uses negative values for the Pacific (it should — the parser normalizes).
