# Cloud Build CI/CD Pipeline

The final infrastructure step. After this, every `git push` to `main` automatically builds the container, pushes it to Artifact Registry, and deploys a new revision to Cloud Run.

> Run all commands from **Cloud Shell**. Some steps require the GCP Console UI for OAuth flows.

---

## What you're building

```
GitHub (push to main)
   │
   ▼
Cloud Build trigger fires
   │
   ▼ runs as sailline-cloudbuild SA
cloudbuild.yaml steps:
   1. docker build -t .../api:$SHA .
   2. docker push .../api:$SHA
   3. gcloud run deploy --image .../api:$SHA
   │
   ▼
Cloud Run revision live at https://sailline-api-xxx-uc.a.run.app
```

---

## Prerequisites

- `06-secrets-and-iam.md` complete (Cloud Build SA + Artifact Registry both exist)
- The backend Dockerfile builds and runs correctly (verified in `05-`)
- Your code is pushed to GitHub at `https://github.com/GraysonLee08/SailLine`

---

## Step 1 — Create the cloudbuild.yaml in the repo

This file lives in your **repo** at `infra/cloudbuild.yaml`, not in Cloud Shell. The full content goes there. Cloud Build reads it whenever a build runs.

```yaml
# infra/cloudbuild.yaml
steps:
  # 1. Build the container image
  - name: 'gcr.io/cloud-builders/docker'
    id: 'build'
    args:
      - 'build'
      - '-t'
      - 'us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:$SHORT_SHA'
      - '-t'
      - 'us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:latest'
      - './backend'

  # 2. Push both tags to Artifact Registry
  - name: 'gcr.io/cloud-builders/docker'
    id: 'push-sha'
    args:
      - 'push'
      - 'us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:$SHORT_SHA'

  - name: 'gcr.io/cloud-builders/docker'
    id: 'push-latest'
    args:
      - 'push'
      - 'us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:latest'

  # 3. Deploy to Cloud Run with VPC connector and runtime service account
  - name: 'gcr.io/google.com/cloudsdktool/cloud-sdk'
    id: 'deploy'
    entrypoint: gcloud
    args:
      - 'run'
      - 'deploy'
      - 'sailline-api'
      - '--image=us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:$SHORT_SHA'
      - '--region=us-central1'
      - '--platform=managed'
      - '--service-account=sailline-api@$PROJECT_ID.iam.gserviceaccount.com'
      - '--vpc-connector=sailline-connector'
      - '--vpc-egress=private-ranges-only'
      - '--allow-unauthenticated'
      - '--port=8080'
      - '--memory=512Mi'
      - '--cpu=1'
      - '--min-instances=0'
      - '--max-instances=10'
      - '--set-env-vars=GCP_PROJECT_ID=$PROJECT_ID,DB_NAME=sailline_app,DB_USER=sailline,DB_HOST=10.69.0.3,REDIS_HOST=10.69.0.4,REDIS_PORT=6379,GCS_WEATHER_BUCKET=sailline-weather'

# Use the dedicated build service account
serviceAccount: 'projects/$PROJECT_ID/serviceAccounts/sailline-cloudbuild@$PROJECT_ID.iam.gserviceaccount.com'

# Send build logs to Cloud Logging only (default tries Cloud Storage which requires extra setup)
options:
  logging: CLOUD_LOGGING_ONLY

images:
  - 'us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:$SHORT_SHA'
  - 'us-central1-docker.pkg.dev/$PROJECT_ID/sailline/api:latest'

timeout: 1200s
```

> **Replace `10.69.0.3` and `10.69.0.4`** with your actual private IPs from `gcloud sql instances describe` and `gcloud redis instances describe`. The values shown match the doc earlier; double-check yours.

Commit and push this file to your repo before Step 2:

```powershell
# On your local machine
cd E:\Personal\Coding\SailLine
git add infra/cloudbuild.yaml
git commit -m "Add Cloud Build pipeline config"
git push origin main
```

---

## Step 2 — Connect the GitHub repo to Cloud Build

This step requires the GCP Console (OAuth flow can't be done via CLI):

1. Go to [console.cloud.google.com/cloud-build/triggers](https://console.cloud.google.com/cloud-build/triggers)
2. Make sure project is set to `sailline` (top-left dropdown)
3. Click **Connect Repository**
4. Source: **GitHub (Cloud Build GitHub App)**
5. Click **Continue**
6. Authenticate with GitHub if prompted, then **Install Google Cloud Build** on your account or org
7. Select the repository `GraysonLee08/SailLine`
8. Accept the consent terms
9. Click **Connect**

You'll be redirected back to the Cloud Build console with the repo now connected. Don't click "Create Trigger" yet — we'll do that next via CLI for repeatability.

---

## Step 3 — Create the build trigger via CLI

```bash
gcloud builds triggers create github \
    --name="sailline-api-deploy" \
    --repo-name="SailLine" \
    --repo-owner="GraysonLee08" \
    --branch-pattern="^main$" \
    --build-config="infra/cloudbuild.yaml" \
    --description="Build and deploy sailline-api on push to main"
```

Verify:

```bash
gcloud builds triggers list --filter="name:sailline-api-deploy"
```

Should show one trigger.

---

## Step 4 — Trigger the first build manually

Don't wait for a push — kick off the first build manually so you can watch it and catch issues early:

```bash
gcloud builds triggers run sailline-api-deploy --branch=main
```

Output gives you a build ID like `abc123-def456-...`. Watch progress:

```bash
gcloud builds list --limit=5
```

The build status goes through `QUEUED` → `WORKING` → `SUCCESS` (or `FAILURE`). First build typically takes **5–8 minutes** because it does a fresh `pip install` of cfgrib, xarray, and numpy.

To stream the logs live:

```bash
# Get the build ID from the previous list command, then:
gcloud builds log <BUILD_ID> --stream
```

Or watch in the Console: [console.cloud.google.com/cloud-build/builds](https://console.cloud.google.com/cloud-build/builds)

---

## Step 5 — Verify the Cloud Run service is live

After the build succeeds, the deploy step creates a new Cloud Run service. Get its URL:

```bash
gcloud run services describe sailline-api --region=us-central1 --format='value(status.url)'
```

Output: `https://sailline-api-xxxxxxxxxx-uc.a.run.app`

Test it:

```bash
curl https://sailline-api-xxxxxxxxxx-uc.a.run.app/health
```

Should return:

```json
{"status":"ok","cfgrib_available":true,"cfgrib_version":"0.9.14.1","cfgrib_error":null}
```

The same `/health` response you saw locally in Cloud Shell — but now served from a managed Cloud Run instance, autoscaling, on HTTPS. **This is your live API.**

---

## Step 6 — Test the auto-deploy loop

The whole point of CI/CD is that you never run `gcloud builds triggers run` manually again. Test the loop:

1. Make a trivial change locally — e.g. update the version string in `backend/app/main.py`:
   ```python
   version="0.1.1",
   ```

2. Commit and push:
   ```powershell
   git add backend/app/main.py
   git commit -m "Bump version to test auto-deploy"
   git push origin main
   ```

3. Within ~30 seconds, a build should kick off. Watch in the console or run:
   ```bash
   gcloud builds list --limit=1
   ```

4. After ~5 minutes, the new revision is live. Re-test:
   ```bash
   curl https://sailline-api-xxxxxxxxxx-uc.a.run.app/
   # Should now show "version": "0.1.1"
   ```

If that worked: **Week 1 infrastructure is fully complete.** Every commit auto-ships.

---

## Verification checklist

```bash
# Trigger exists and is enabled
gcloud builds triggers describe sailline-api-deploy --format="value(disabled)"
# Should print empty/null (not "True")

# Latest build succeeded
gcloud builds list --limit=1 --format="value(status)"
# Should be: SUCCESS

# Cloud Run service exists
gcloud run services describe sailline-api --region=us-central1 --format="value(status.conditions[0].type,status.conditions[0].status)"
# Should be: Ready    True

# /health returns cfgrib true
curl -s $(gcloud run services describe sailline-api --region=us-central1 --format='value(status.url)')/health
```

---

## Troubleshooting

### Build fails at the deploy step with "permission denied on iam.serviceAccounts.actAs"
You're missing the `iam.serviceAccountUser` binding from `06-secrets-and-iam.md` Step 3. Re-run that step.

### Build fails with "VPC connector not found"
Either the connector name in `cloudbuild.yaml` doesn't match (should be `sailline-connector`) or the connector isn't in the same region as Cloud Run.

### Deploy succeeds but `/health` returns 500
Cloud Run is running, but the container is crashing. Check logs:
```bash
gcloud run services logs read sailline-api --region=us-central1 --limit=50
```
Most common cause at this stage: env var mismatch (private IPs hardcoded in `cloudbuild.yaml` are wrong).

### Build fails downloading cfgrib
Cloud Build's network is sometimes flaky. Re-run:
```bash
gcloud builds triggers run sailline-api-deploy --branch=main
```

### "Container failed to start. Failed to start and listen on port"
Usually means the app raised an exception on import. Check Cloud Run logs (`gcloud run services logs read ...`). At this stage the most likely cause is a typo in `app/main.py` or a missing dependency in `requirements.txt`.

### First push didn't trigger a build
The Cloud Build GitHub App needs read access to the repo. Go back to [github.com/settings/installations](https://github.com/settings/installations), click the Cloud Build app, and confirm SailLine is in the repository access list.

---

## What's now true

After completing this doc:

- ✅ `git push origin main` automatically builds and deploys
- ✅ The FastAPI app is live on Cloud Run with HTTPS
- ✅ The container is running with `cfgrib` available
- ✅ Cloud Run can reach Cloud SQL (private IP) via the VPC connector
- ✅ Cloud Run can read Memorystore (private IP) via the VPC connector
- ✅ The runtime SA has access to Cloud Storage and Secret Manager

What's NOT yet wired up (and will be in their respective build weeks):

- ❌ Database connection from FastAPI (Week 1 — straightforward, just import asyncpg and connect)
- ❌ Firebase JWT verification (Week 1 — install firebase-admin, write the dependency)
- ❌ Weather worker as a Cloud Run Job (Week 2)
- ❌ AIS, Stripe, Claude integrations (Weeks 7–8)

---

## What's next

Week 1 infrastructure is done. **You're ready to start writing application code.**

The recommended order from the build plan:
1. Wire up Cloud SQL connection in FastAPI (`app/db.py` + a real query in `/health`)
2. Add Firebase JWT verification (`app/auth.py` + a protected endpoint)
3. Move on to Week 2: weather data pipeline (the GRIB2 worker)

After that, you stop touching infrastructure and start shipping product features.
