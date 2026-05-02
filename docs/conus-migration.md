# CONUS + Venue Migration Runbook

This swaps the existing 19 region-specific Cloud Run Jobs for a new architecture: 2 base jobs (CONUS HRRR + GFS, Hawaii GFS) plus 15 venue jobs (high-res HRRR over popular sailing areas).

**Why:** the old per-region grids had hard boundaries — pan from Chicago to Florida and the wind disappeared. The new model keeps CONUS always-on so wind shows everywhere a US user might pan, with native-resolution HRRR overlays at popular venues for buoy-racing detail.

**Time:** ~30 minutes. Most of it is `gcloud run jobs create` + first-run executions.

**Net job count:** 19 → 18. Cost roughly flat (~$35–40/mo).

---

## Pre-flight

```bash
PROJECT_ID=$(gcloud config get-value project)
REDIS_IP=$(gcloud redis instances describe sailline-cache --region=us-central1 --format='value(host)')
SCHEDULER_SA="sailline-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
INGEST_SA="sailline-api@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="us-central1-docker.pkg.dev/${PROJECT_ID}/sailline/api:latest"
API_URL=$(gcloud run services describe sailline-api --region=us-central1 --format='value(status.url)')

echo "PROJECT_ID=$PROJECT_ID  REDIS_IP=$REDIS_IP"
echo "API_URL=$API_URL"
```

Verify the new code is deployed:

```bash
# 503 expected (region known, no data yet) — NOT 404 (would mean old code)
curl -is "$API_URL/api/weather?region=conus&source=hrrr" -o /dev/null -w "conus hrrr: %{http_code}\n"
```

If you get 404, Cloud Build hasn't finished. Wait and retry.

---

## Step 1 — Tear down the old per-region jobs and triggers

The old setup had 19 jobs for great_lakes + chesapeake + sf_bay + ... + hawaii. Names there don't match the new registry, so delete them.

```bash
OLD_JOBS=(
  sailline-ingest-hrrr  # legacy great_lakes
  sailline-ingest-gfs   # legacy great_lakes
  sailline-ingest-hrrr-chesapeake          sailline-ingest-gfs-chesapeake
  sailline-ingest-hrrr-long-island-sound   sailline-ingest-gfs-long-island-sound
  sailline-ingest-hrrr-new-england         sailline-ingest-gfs-new-england
  sailline-ingest-hrrr-florida             sailline-ingest-gfs-florida
  sailline-ingest-hrrr-gulf-coast          sailline-ingest-gfs-gulf-coast
  sailline-ingest-hrrr-socal               sailline-ingest-gfs-socal
  sailline-ingest-hrrr-sf-bay              sailline-ingest-gfs-sf-bay
  sailline-ingest-hrrr-pnw                 sailline-ingest-gfs-pnw
  sailline-ingest-gfs-hawaii
)

OLD_TRIGGERS=(
  sailline-trigger-hrrr
  sailline-trigger-gfs
  sailline-trigger-hrrr-chesapeake          sailline-trigger-gfs-chesapeake
  sailline-trigger-hrrr-long-island-sound   sailline-trigger-gfs-long-island-sound
  sailline-trigger-hrrr-new-england         sailline-trigger-gfs-new-england
  sailline-trigger-hrrr-florida             sailline-trigger-gfs-florida
  sailline-trigger-hrrr-gulf-coast          sailline-trigger-gfs-gulf-coast
  sailline-trigger-hrrr-socal               sailline-trigger-gfs-socal
  sailline-trigger-hrrr-sf-bay              sailline-trigger-gfs-sf-bay
  sailline-trigger-hrrr-pnw                 sailline-trigger-gfs-pnw
  sailline-trigger-gfs-hawaii
)

# Delete triggers first so nothing fires during the cutover.
for t in "${OLD_TRIGGERS[@]}"; do
  gcloud scheduler jobs delete "$t" --location=us-central1 --quiet 2>/dev/null \
    && echo "  deleted trigger $t" \
    || echo "  (trigger $t not found, skipping)"
done

# Then delete the jobs themselves.
for j in "${OLD_JOBS[@]}"; do
  gcloud run jobs delete "$j" --region=us-central1 --quiet 2>/dev/null \
    && echo "  deleted job $j" \
    || echo "  (job $j not found, skipping)"
done
```

Stale Redis keys (`weather:hrrr:great_lakes:latest`, etc.) will TTL out within their cycle window — no action needed. Stale GCS objects under the old region prefixes can be cleaned up later or left to lifecycle policy.

---

## Step 2 — Create the new jobs

The new matrix is 18 jobs:

| Region | HRRR | GFS | Notes |
|---|---|---|---|
| conus | 0.10° | 0.25° | base |
| hawaii | — | 0.25° | base, no HRRR |
| 15 venues | 0.027° | — | HRRR-only |

```bash
PAIRS=(
  # source region
  "hrrr conus"
  "gfs  conus"
  "gfs  hawaii"
  # venues — HRRR only
  "hrrr chicago"
  "hrrr milwaukee"
  "hrrr detroit"
  "hrrr cleveland"
  "hrrr sf_bay"
  "hrrr long_beach"
  "hrrr san_diego"
  "hrrr puget_sound"
  "hrrr annapolis"
  "hrrr newport_ri"
  "hrrr buzzards_bay"
  "hrrr marblehead"
  "hrrr charleston"
  "hrrr biscayne_bay"
  "hrrr corpus_christi"
)

for pair in "${PAIRS[@]}"; do
  read -r src region <<< "$pair"
  region_dash=${region//_/-}
  job_name="sailline-ingest-${src}-${region_dash}"

  # CONUS HRRR is the heaviest job — full ~1.9M source point regrid.
  # Bump memory/CPU/timeout for that one only.
  if [ "$src" = "hrrr" ] && [ "$region" = "conus" ]; then
    memory="4Gi"; cpu="2"; timeout="20m"
  else
    memory="1Gi"; cpu="1"; timeout="10m"
  fi

  echo "Creating ${job_name} (mem=${memory})..."

  gcloud run jobs create "${job_name}" \
    --image="${IMAGE}" \
    --region=us-central1 \
    --service-account="${INGEST_SA}" \
    --vpc-connector=sailline-connector \
    --vpc-egress=private-ranges-only \
    --task-timeout="${timeout}" \
    --memory="${memory}" \
    --cpu="${cpu}" \
    --max-retries=1 \
    --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},DB_NAME=sailline_app,DB_USER=sailline,REDIS_HOST=${REDIS_IP},REDIS_PORT=6379,GCS_WEATHER_BUCKET=sailline-weather" \
    --command="python" \
    --args="-m,workers.weather_ingest,${src},--region,${region}"
done
```

CONUS HRRR memory bump explained: HRRR's native LCC grid is ~1799 × 1059 = 1.9M points. The scipy Delaunay triangulation used to regrid onto our 0.10° lat/lon target needs working memory proportional to source point count plus output point count (~156k for CONUS). 4 GB is comfortable headroom; 2 GB might OOM on edge cases.

---

## Step 3 — First-run each job

Otherwise users hitting these regions get 503 until the first scheduled run.

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

Spot-check after they finish:

```bash
curl -is "$API_URL/api/weather?region=conus&source=hrrr"   -o /dev/null -w "conus  hrrr: %{http_code}\n"
curl -is "$API_URL/api/weather?region=conus&source=gfs"    -o /dev/null -w "conus  gfs:  %{http_code}\n"
curl -is "$API_URL/api/weather?region=hawaii&source=gfs"   -o /dev/null -w "hawaii gfs:  %{http_code}\n"
curl -is "$API_URL/api/weather?region=sf_bay&source=hrrr"  -o /dev/null -w "sf_bay hrrr: %{http_code}\n"
curl -is "$API_URL/api/weather?region=chicago&source=hrrr" -o /dev/null -w "chicago hrrr:%{http_code}\n"
```

All should be 200. If any are 503, check that job's execution log:

```bash
gcloud run jobs executions list --job=sailline-ingest-hrrr-conus --region=us-central1 --limit=3
gcloud run jobs executions logs read <execution-id> --region=us-central1
```

---

## Step 4 — Schedule each job

HRRR runs hourly (5 minutes past, accounting for ~2h publish lag). GFS runs every 6 hours (30 minutes past 05/11/17/23 UTC, accounting for ~5h publish lag).

```bash
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
    --time-zone=UTC \
    --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/us-central1/jobs/${job_name}:run" \
    --http-method=POST \
    --oauth-service-account-email="${SCHEDULER_SA}" \
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
done
```

Verify:

```bash
gcloud scheduler jobs list --location=us-central1 --filter="name:sailline-trigger" \
  --format="table(name.basename(),schedule,state)"
```

You should see exactly 18 triggers, all ENABLED.

---

## Step 5 — Frontend cutover

Frontend lives behind Firebase Hosting; deploy proceeds independently of the backend rollout. The frontend code now requests `region=conus` (or `region=hawaii`, or one of the 15 venue names) — none of which existed before. As long as steps 1–4 above are complete, the API returns 200 for these. Push the frontend.

After deploy, sanity-check in the browser:

1. Open the app cold (incognito to defeat localStorage). Map should auto-detect → CONUS, fly to its center, render barbs everywhere.
2. Pan to Florida, Texas, California — barbs should fill the whole CONUS.
3. Zoom into San Francisco Bay (zoom 12+). Barb density should visibly increase as the venue overlay activates.
4. Pan from the Bay east into Sacramento (still zoom 12) — barbs should drop back to base CONUS density once the viewport center leaves the venue bbox.

If the venue overlay doesn't show:

- Verify `viewport.zoom >= VENUE_ZOOM_THRESHOLD` (= 11).
- Open DevTools Network → confirm a request to `/api/weather?region=sf_bay&source=hrrr` fires when you zoom in.
- Confirm that response is 200, not 503.

---

## Cost notes

| Item | Old (19 jobs) | New (18 jobs) | Notes |
|---|---|---|---|
| Cloud Run Jobs runtime | ~$40/mo | ~$45/mo | CONUS HRRR is heavier than a regional job, but venues are smaller — net roughly flat |
| GCS storage | ~$0.30/mo | ~$0.30/mo | Object count similar, sizes comparable |
| Memorystore | ~$36/mo | ~$36/mo | Total payload still well under 1 GB |

If costs creep up unexpectedly, the lever is venue cadence — drop sleepy venues from hourly to every 3 hours.

---

## Troubleshooting

**CONUS HRRR job runs out of memory or times out**
This is the only "heavy" job. If 4 GB / 20 min isn't enough, bump to 8 GB / 30 min. Beyond that, consider subsampling HRRR's native grid before regridding (would require a code change in `app/services/grib._regrid_curvilinear`).

**Venue ingest succeeds but `/api/weather?region={venue}` returns 503**
The Redis key shape is `weather:hrrr:{venue_name}:latest`. Connect via the bastion or read-from-API-pod and `GET` that key. If empty, the worker isn't reaching Redis (check VPC connector + REDIS_HOST env var). If populated, the API isn't reading it (check API pod's REDIS_HOST matches the same IP).

**User in Hawaii sees CONUS barbs**
Hawaii base region's bbox is (18.5–22.5°N, -161 to -154.5°W). If detection IP-geolocates to Honolulu but the test user is panning to a Pacific point outside that bbox, `baseRegionForPoint` returns null and the code falls back to DEFAULT_BASE_REGION (conus). If you're testing with VPN/spoofed location, double-check the actual GPS coordinates being returned.

**Detroit/Cleveland venue grid is empty**
HRRR's CONUS domain extends to ~-60°W on the East Coast. Both Detroit (-83°W) and Cleveland (-82°W) are well within. Empty grids here would indicate an idx-parse issue, not a coverage one. Check the worker log for `[hrrr/detroit] grid 0x0` — if so, the bbox-buffer interaction in `_regrid_curvilinear` may have edge-cased.
