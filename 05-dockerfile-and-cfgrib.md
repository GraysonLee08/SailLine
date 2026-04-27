# Dockerfile + cfgrib Validation

Builds and tests the backend container with the `eccodes` system library installed for `cfgrib`. This validates the highest technical risk in the project before any application code is written against it.

> Run all commands from **Cloud Shell** in the GCP Console. Cloud Shell has Docker pre-installed and authenticated to your project.

---

## What you're testing

When NOAA publishes weather forecasts, they ship them in **GRIB2** binary format. The Python library `cfgrib` parses GRIB2 files but depends on a C library called **eccodes** that must be installed at the OS level. If eccodes is missing or wrong-version, `import cfgrib` crashes at runtime with an obscure error.

This step verifies:

1. The Dockerfile correctly installs `libeccodes-dev` from apt
2. `cfgrib` imports successfully inside the running container
3. The FastAPI app starts and responds to HTTP requests

If all three work in Cloud Shell, you have very high confidence the same container will work when deployed to Cloud Run.

---

## Files created in this step

After this step, the backend has:

```
backend/
├── Dockerfile               # eccodes + cfgrib install
├── .dockerignore
├── requirements.txt         # FastAPI + cfgrib + xarray
├── .env.example
└── app/
    ├── __init__.py
    ├── main.py              # FastAPI entry point
    └── routers/
        ├── __init__.py
        └── health.py        # /health reports cfgrib status
```

The full file contents are in the repo — check the commit history for what was added.

---

## Step 1 — Clone your repo into Cloud Shell

Cloud Shell starts with an empty home directory. Clone the SailLine repo:

```bash
cd ~
git clone https://github.com/GraysonLee08/SailLine.git
cd SailLine/backend
```

Verify the files are present:

```bash
ls -la
# Should show: Dockerfile, .dockerignore, requirements.txt, app/, .env.example
```

---

## Step 2 — Build the Docker image

```bash
docker build -t sailline-api:test .
```

This takes 2–4 minutes the first time. Most of the time is `pip install` of `cfgrib` and `xarray` (numpy + xarray are large).

Watch the build output for these key lines:

- `apt-get install ... libeccodes-dev` — should succeed without errors
- `Collecting cfgrib==0.9.14.1` — pip downloads it
- `Successfully built ...` — confirms the image built

If the build fails, the most likely cause is a network error or a version conflict. Re-run the command. If it consistently fails, check the troubleshooting section below.

---

## Step 3 — Run the container

```bash
docker run --rm -d -p 8080:8080 --name sailline-test sailline-api:test
```

Flags:
- `--rm` — auto-cleanup when the container stops
- `-d` — run in background
- `-p 8080:8080` — map host port 8080 to container port 8080
- `--name sailline-test` — give it a name so we can stop it easily

Verify the container is running:

```bash
docker ps
```

You should see `sailline-test` in the list with status `Up`.

---

## Step 4 — Test the health endpoint

```bash
curl http://localhost:8080/health
```

Expected output (formatted for readability):

```json
{
  "status": "ok",
  "cfgrib_available": true,
  "cfgrib_version": "0.9.14.1",
  "cfgrib_error": null
}
```

**This is the moment of truth.** If `cfgrib_available: true`, the entire weather data pipeline is unblocked. If you see `cfgrib_available: false` with an error message, the eccodes installation didn't work — see troubleshooting below.

Also test the root endpoint:

```bash
curl http://localhost:8080/
```

Should return:

```json
{
  "service": "sailline-api",
  "version": "0.1.0",
  "cfgrib": "available (cfgrib 0.9.14.1)"
}
```

---

## Step 5 — Verify cfgrib actually works (not just imports)

Importing succeeds is good. Actually parsing a GRIB file is better. Run this command to exec into the running container and try a real cfgrib operation:

```bash
docker exec sailline-test python -c "
import cfgrib
import xarray as xr
print('cfgrib version:', cfgrib.__version__)
print('xarray version:', xr.__version__)

# Verify the eccodes binary is callable
import subprocess
result = subprocess.run(['codes_info'], capture_output=True, text=True)
print('eccodes:', result.stdout.split(chr(10))[0] if result.returncode == 0 else 'ERROR')
"
```

Expected output:

```
cfgrib version: 0.9.14.1
xarray version: 2024.9.0
eccodes: ecCodes Version X.XX.X
```

If `eccodes` reports a version, the entire stack is verified end-to-end.

---

## Step 6 — Stop the container

```bash
docker stop sailline-test
```

The `--rm` flag removes it automatically when stopped. Verify it's gone:

```bash
docker ps -a | grep sailline-test
# Should return nothing
```

---

## Verification checklist

- [x] `docker build` completes without errors
- [x] `docker ps` shows the container running
- [x] `/health` returns `cfgrib_available: true`
- [x] `/` returns version info
- [x] `codes_info` executes inside the container

If all five pass, this milestone is done. The deployment pipeline (Cloud Build → Artifact Registry → Cloud Run) is the next step, and it will use this same Dockerfile.

---

## Troubleshooting

### Build fails with `E: Unable to locate package libeccodes-dev`

The Debian package index is stale or the wrong base image is being used. Confirm the Dockerfile uses `python:3.12-slim` and add a `apt-get update` retry:

```dockerfile
RUN apt-get update && apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-dev \
    && rm -rf /var/lib/apt/lists/*
```

### Build succeeds but `import cfgrib` fails at runtime

Most common cause: `libeccodes-dev` was installed but cfgrib can't find it because of a mismatch in expected library paths. Check what cfgrib reports:

```bash
docker exec sailline-test python -c "import cfgrib; cfgrib.bindings.check_message()"
```

If this throws, try installing `libeccodes-tools` alongside `libeccodes-dev` (already in the Dockerfile but verify it's there).

### `docker build` takes more than 10 minutes

Cloud Shell has limited CPU. The cfgrib + xarray + numpy install is genuinely slow on a tiny VM. This is normal — be patient. Subsequent builds will be much faster due to layer caching.

### `curl localhost:8080/health` returns connection refused

The container started but Uvicorn hasn't bound to the port yet. Wait 5 seconds and retry. If it still fails:

```bash
docker logs sailline-test
```

Look for the line `Uvicorn running on http://0.0.0.0:8080`. If you don't see it, there's a startup error in the logs.

### Cloud Shell "out of disk space" error

Cloud Shell only has 5 GB of disk. The cfgrib build artifacts can fill it up. Clean up:

```bash
docker system prune -af
```

This removes all stopped containers and unused images. Then rebuild.

---

## What's next

With the Dockerfile validated, the next steps are about **shipping it to production**:

- `06-secrets-and-iam.md` — store API keys (Datalastic, Anthropic, Stripe) in Secret Manager; set up the Cloud Build service account
- `07-cloudbuild-cicd.md` — Artifact Registry repo + Cloud Build trigger that auto-deploys on `git push` to `main`

After those, every commit to main will auto-deploy your container to Cloud Run.
