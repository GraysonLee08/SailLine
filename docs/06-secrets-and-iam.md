# Secrets + IAM (Cloud Build, Artifact Registry, API keys)

Sets up the IAM scaffolding needed for the Cloud Build → Artifact Registry → Cloud Run pipeline, and documents the pattern for managing external API keys (Datalastic, Anthropic, Stripe) when you have them.

> All commands run in **Cloud Shell (bash)**. Region: `us-central1`.

---

## What you're building

```
Cloud Build (CI/CD)
   │
   │ runs as → sailline-cloudbuild SA
   │
   ├─→ pushes container images to Artifact Registry
   └─→ deploys revisions to Cloud Run
                                │
                                │ runs as → sailline-api SA
                                │
                                ├─→ reads secrets from Secret Manager
                                ├─→ connects to Cloud SQL
                                └─→ reads/writes Cloud Storage bucket
```

Two service accounts, distinct purposes:

| Account | Purpose | Permissions needed |
|---|---|---|
| `sailline-api` | Runs the FastAPI container in Cloud Run | DB, secrets, storage, Firebase Auth (already granted) |
| `sailline-cloudbuild` | Builds + deploys the container | Artifact Registry write, Cloud Run admin, act-as runtime SA |

Separating them follows the principle of least privilege — the runtime account can't deploy itself, and the build account can't read app secrets.

---

## Prerequisites

- `05-dockerfile-and-cfgrib.md` complete (Dockerfile validated)
- `sailline-api` service account already exists with DB/secrets/storage roles

---

## Part A: Cloud Build service account

### Step 1 — Create the dedicated build service account

```bash
gcloud iam service-accounts create sailline-cloudbuild \
    --display-name="SailLine Cloud Build deployer"
```

The full email of this account is `sailline-cloudbuild@sailline.iam.gserviceaccount.com`.

### Step 2 — Grant build + deploy permissions

Three roles, granted at the project level:

```bash
# Push container images to Artifact Registry
gcloud projects add-iam-policy-binding sailline \
    --member="serviceAccount:sailline-cloudbuild@sailline.iam.gserviceaccount.com" \
    --role="roles/artifactregistry.writer"

# Deploy revisions to Cloud Run
gcloud projects add-iam-policy-binding sailline \
    --member="serviceAccount:sailline-cloudbuild@sailline.iam.gserviceaccount.com" \
    --role="roles/run.admin"

# Read Cloud Build logs (helps debugging from CLI)
gcloud projects add-iam-policy-binding sailline \
    --member="serviceAccount:sailline-cloudbuild@sailline.iam.gserviceaccount.com" \
    --role="roles/logging.logWriter"
```

### Step 3 — Allow Cloud Build to "act as" the runtime service account

When Cloud Build deploys a Cloud Run service, it has to attach the runtime service account (`sailline-api`) to the new revision. To do that, Cloud Build's identity needs `Service Account User` on the runtime SA:

```bash
gcloud iam service-accounts add-iam-policy-binding \
    sailline-api@sailline.iam.gserviceaccount.com \
    --member="serviceAccount:sailline-cloudbuild@sailline.iam.gserviceaccount.com" \
    --role="roles/iam.serviceAccountUser"
```

This is **per-service-account scoped**, not project-wide. The build account can attach the API runtime SA but not any other service account.

### Step 4 — Verify

```bash
gcloud projects get-iam-policy sailline \
    --flatten="bindings[].members" \
    --format="value(bindings.role)" \
    --filter="bindings.members:sailline-cloudbuild@sailline.iam.gserviceaccount.com"
```

Expected three lines:

```
roles/artifactregistry.writer
roles/logging.logWriter
roles/run.admin
```

And the SA-scoped binding:

```bash
gcloud iam service-accounts get-iam-policy \
    sailline-api@sailline.iam.gserviceaccount.com \
    --format="value(bindings.role)" \
    --filter="bindings.members:sailline-cloudbuild@sailline.iam.gserviceaccount.com"
```

Expected: `roles/iam.serviceAccountUser`

---

## Part B: Artifact Registry repository

### Step 5 — Create the Docker repository

Artifact Registry holds your container images. Each project needs at least one repository.

```bash
gcloud artifacts repositories create sailline \
    --repository-format=docker \
    --location=us-central1 \
    --description="SailLine container images"
```

### Step 6 — Allow the runtime SA to pull images

Cloud Run needs to pull the image when starting a new revision. Grant `Artifact Registry Reader` to the runtime account, scoped to just this repository:

```bash
gcloud artifacts repositories add-iam-policy-binding sailline \
    --location=us-central1 \
    --member="serviceAccount:sailline-api@sailline.iam.gserviceaccount.com" \
    --role="roles/artifactregistry.reader"
```

### Step 7 — Verify

```bash
gcloud artifacts repositories list --location=us-central1
```

Expected output should include `sailline` with format `DOCKER`.

The full image URL format you'll use in `cloudbuild.yaml` is:

```
us-central1-docker.pkg.dev/sailline/sailline/api:<tag>
```

Save that — you'll need it in the next doc.

---

## Part C: Secret management pattern

You don't have Datalastic, Anthropic, or Stripe keys yet — those services get signed up in their respective build weeks. **Don't create placeholder secrets now.** This section documents the pattern so you can do it correctly when each key arrives.

### Naming convention

| Secret name | Holds | Created in week |
|---|---|---|
| `sailline-db-postgres-password` | Postgres admin password | ✅ already exists |
| `sailline-db-app-password` | App user password | ✅ already exists |
| `sailline-datalastic-api-key` | Datalastic AIS API key | Week 7 |
| `sailline-anthropic-api-key` | Anthropic Claude API key | Week 8 |
| `sailline-stripe-secret-key` | Stripe API secret | Week 7 |
| `sailline-stripe-webhook-secret` | Stripe webhook signing secret | Week 7 |

Pattern: `sailline-<vendor>-<purpose>`. Hyphens, lowercase, no underscores.

### Creating a secret

When you sign up for a service and have a real key, create the secret like this:

```bash
# Replace with the actual key value
echo -n "your-actual-api-key-here" | gcloud secrets create sailline-datalastic-api-key --data-file=-
```

The `-n` flag on `echo` is critical — without it, the secret stores a trailing newline that breaks API auth.

### Updating a secret (rotating the key)

To replace the value (e.g., the vendor rotated your key, or you're moving from a test key to a production key):

```bash
echo -n "new-api-key-value" | gcloud secrets versions add sailline-datalastic-api-key --data-file=-
```

This adds a new version; the old version is still retrievable but Cloud Run pulls `latest` by default.

### Reading a secret value

```bash
gcloud secrets versions access latest --secret=sailline-datalastic-api-key
```

### Wiring a secret into Cloud Run

When deploying, bind the secret to an environment variable:

```bash
gcloud run deploy sailline-api \
    --update-secrets="DATALASTIC_API_KEY=sailline-datalastic-api-key:latest" \
    ...
```

Inside the FastAPI app, the secret is just an env var: `os.environ["DATALASTIC_API_KEY"]`.

### Granting access (already done at project level)

The `sailline-api` service account has `roles/secretmanager.secretAccessor` at the project level (granted in `03-vpc-and-cloud-sql.md`). That means it can read **any** secret in the project. This is fine for solo-developer simplicity but if you want tighter security, replace the project-wide binding with per-secret bindings:

```bash
# Optional — tighter security, more setup
gcloud secrets add-iam-policy-binding sailline-datalastic-api-key \
    --member="serviceAccount:sailline-api@sailline.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
```

For v1, project-wide access is acceptable.

---

## Verification checklist

```bash
# Cloud Build SA exists
gcloud iam service-accounts list --filter="email:sailline-cloudbuild@*" --format="value(email)"

# Cloud Build SA has the right project roles
gcloud projects get-iam-policy sailline \
    --flatten="bindings[].members" \
    --format="value(bindings.role)" \
    --filter="bindings.members:sailline-cloudbuild@sailline.iam.gserviceaccount.com" | sort

# Cloud Build SA can act-as runtime SA
gcloud iam service-accounts get-iam-policy \
    sailline-api@sailline.iam.gserviceaccount.com \
    --format="value(bindings.role)" \
    --filter="bindings.members:sailline-cloudbuild@sailline.iam.gserviceaccount.com"

# Artifact Registry repo exists
gcloud artifacts repositories describe sailline --location=us-central1 --format="value(name,format)"

# Runtime SA can read from the repo
gcloud artifacts repositories get-iam-policy sailline --location=us-central1 \
    --format="value(bindings)" | grep sailline-api
```

Expected (in order):

```
sailline-cloudbuild@sailline.iam.gserviceaccount.com
roles/artifactregistry.writer
roles/logging.logWriter
roles/run.admin
roles/iam.serviceAccountUser
projects/sailline/locations/us-central1/repositories/sailline    DOCKER
serviceAccount:sailline-api@sailline.iam.gserviceaccount.com
```

---

## What's next

With Cloud Build IAM and Artifact Registry in place, the final infrastructure step is the actual CI/CD pipeline:

- `07-cloudbuild-cicd.md` — `cloudbuild.yaml` config + GitHub trigger that auto-deploys on push to `main`

After that, Week 1 infrastructure is fully done and every commit auto-ships to Cloud Run.
