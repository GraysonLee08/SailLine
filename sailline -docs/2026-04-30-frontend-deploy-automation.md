# Frontend Deploy Automation — Session Summary

Date: 2026-04-30

## Goal

Auto-deploy the frontend to Firebase Hosting on push to `main`, mirroring the existing backend Cloud Build trigger. Estimated 15 minutes of work; actual ~45 minutes due to two surprises (Windows-generated lockfile, org policy on triggers).

## What shipped

A second Cloud Build pipeline runs alongside the backend one. Pushes to `main` now build the React app and deploy it to Firebase Hosting automatically. As a bonus, all frontend → backend traffic now goes same-origin through a Firebase Hosting rewrite, eliminating CORS entirely and removing the hardcoded Cloud Run URL from the frontend bundle.

## Files changed in the repo

- `frontend/firebase.json` — added `/api/**` → Cloud Run `sailline-api` rewrite as the first rule (must come before the SPA catch-all)
- `frontend/src/api.js` — `API_URL` fallback changed from the hardcoded Cloud Run URL to `""`, so production paths are relative
- `frontend/src/hooks/useWeather.js` — same change
- `infra/cloudbuild.frontend.yaml` — new build config: `npm ci` → fetch secret → `vite build` → `firebase deploy --only hosting`
- `frontend/package-lock.json` — regenerated on Linux to fix a rolldown native-binding issue (see "things that surprised us")

## One-time GCP setup performed

```bash
# 1. Hosting deploy permission for the existing build SA
gcloud projects add-iam-policy-binding sailline \
  --member="serviceAccount:sailline-cloudbuild@sailline.iam.gserviceaccount.com" \
  --role="roles/firebasehosting.admin"

# 2. Production VITE_* env vars stored in Secret Manager
gcloud secrets create sailline-frontend-env --replication-policy=automatic
gcloud secrets versions add sailline-frontend-env --data-file=/tmp/frontend.env.production

# 3. Per-secret access for the build SA (least privilege; project-wide
#    secretAccessor was not previously granted to this SA)
gcloud secrets add-iam-policy-binding sailline-frontend-env \
  --member="serviceAccount:sailline-cloudbuild@sailline.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# 4. Trigger created via Console (CLI was rejected — see below)
#    Name: sailline-frontend-deploy
#    Branch: ^main$
#    Config: infra/cloudbuild.frontend.yaml
#    Service account: sailline-cloudbuild@sailline.iam.gserviceaccount.com
```

## Architecture decisions

**Same-origin via Firebase Hosting rewrite (Option B in planning).** Frontend calls relative paths like `/api/users/me`; Firebase Hosting routes anything under `/api/**` to the `sailline-api` Cloud Run service. Eliminates CORS, future-proofs custom-domain swaps, and means we don't have to bake `VITE_API_URL` into the bundle. `VITE_API_URL` stays in `.env.local` for local dev only (`npm run dev` at port 5173 still points at `http://localhost:8080`).

**ADC auth, not Firebase CI tokens.** `firebase-tools` v11.7+ honors Application Default Credentials. Cloud Build provides ADC via the metadata service, so granting `roles/firebasehosting.admin` to the build SA was sufficient — no `firebase login:ci` token to rotate. Worked on the first try.

**Separate yaml, not a merged pipeline.** `infra/cloudbuild.frontend.yaml` is independent of the backend's `cloudbuild.yaml`. Different builder images (node:20 vs docker), different secrets, different failure surfaces. Keeping them separate means a frontend lockfile bug doesn't block a backend deploy and vice versa.

## Things that surprised us

### 1. `INVALID_ARGUMENT` from `gcloud builds triggers create`

The CLI returned a generic `INVALID_ARGUMENT` with no field-level detail, even with `--verbosity=debug`. Diagnostic flags didn't surface the real reason. Switched to the GCP Console UI, which displayed a banner: *"Your organization policy requires you to select a user-managed service account."* Picked `sailline-cloudbuild`, trigger created cleanly.

**Lesson:** when gcloud returns a bare `INVALID_ARGUMENT` and `--verbosity=debug` doesn't help, try the Console — its UI surfaces field-specific errors that the gcloud command swallows.

### 2. `package-lock.json` was generated on Windows; Linux build broke

The first build failed at the `vite build` step with `Cannot find module '@rolldown/binding-linux-x64-gnu'`. This is [npm/cli#4828](https://github.com/npm/cli/issues/4828) — npm's optional-dependencies bug. When `npm install` runs on Windows, the lockfile only references the Windows native binding for rolldown (Vite 8's bundler). `npm ci` on Linux then can't find the Linux binding and the build crashes.

**Fix:** clone the repo in Cloud Shell, `rm package-lock.json`, `npm install`, commit the regenerated lockfile, push. Linux-generated lockfile resolves all platform bindings correctly.

**Lesson:** any Vite-8-or-later project where the lockfile travels between Windows dev machines and Linux CI will hit this. Either commit the lockfile from Linux only (regenerate in WSL or in CI on first push) or pin to a stable bundler. Worth a one-line note in the project README.

### 3. `.env.local` had doubled quotes and CRLF endings

Uploading the local `.env.local` to Cloud Shell exposed two Windows-isms:

- `VITE_FIREBASE_API_KEY=""AIzaSy..."" ` — doubled double-quotes around the value. Vite was lenient enough to accept it locally, but it's a real bug waiting to bite a stricter parser.
- CRLF line endings, which made the first `sed` cleanup pass silently no-op (the regex matched `""$` but the actual string ended with `""\r`).

**Fix:** `sed -i -e 's/\r$//' -e 's/=""\(.*\)""$/="\1"/' /tmp/frontend.env.production` — strip CR first, then collapse the doubled quotes.

**Lesson:** any `.env` file authored on Windows should be normalized before being shipped to a Linux runtime. Worth adding a pre-commit hook or a CI check.

### 4. The first frontend trigger run also re-triggered the backend

Both triggers currently fire on any push to `main` because neither has an `--included-files` filter. Each frontend lockfile push therefore rebuilds the backend container too. Harmless (Cloud Run revisions are cheap and content-addressed by SHA), but wasteful. Fix is in the open items.

## Verification

- **Build `ffdd105d-22e7-47a8-8001-22bee929359c`** at SHA `df4bddc` completed in 1m31s, status SUCCESS.
- `https://sailline.web.app` serves the latest bundle.
- *Pending user verification:* signed-in `/api/users/me` request hits `https://sailline.web.app/api/users/me` (not the Cloud Run URL), returns 200, no `OPTIONS` preflight in the Network tab.
- *Pending user verification:* a trivial frontend-only commit kicks off a new build automatically.

## Open items

1. **Path filters on both triggers.** Add `--included-files="frontend/**"` to `sailline-frontend-deploy` and `--included-files="backend/**,infra/cloudbuild.yaml"` to `sailline-api-deploy`. The `--included-files` flag was the suspected cause of the original `INVALID_ARGUMENT` on `create`, but `update` should work — and falling back to the Console is a known-good escape hatch if it doesn't.
2. **`.env.example` is stale.** Still lists `VITE_API_URL=http://localhost:8080` as a required value. With the rewrite in place, this should be marked optional ("only for local dev — production uses relative paths via Firebase Hosting rewrites").
3. **Schema migration framework.** Carried from the morning session of 2026-04-30. Still the #1 priority before any feature touches the DB.
4. **Bundle splitting.** First-load is 2 MB. Mapbox is the heavy hitter; lazy-loading the `RaceEditor` route would keep the auth/list path light.
5. **Long-distance course presets** in `morfCourses.js` (Zimmer, Skipper's Club, Hammond, etc.). Carried.

## Pre-ship feature backlog

Discussed mid-session, deferred so the deploy automation could ship cleanly:

1. **Map as single pane of glass.** After saving a race, load directly into the map view — make the map the central view of the app, not a destination among several.
2. **Race date + class start time fields with countdown.** Adds fields to race setup; displays a live countdown to start time in the editor. Requires schema changes, so it should land *after* the migration framework.

## Operational notes

- Production frontend env vars live in Secret Manager as `sailline-frontend-env`. To rotate any of them: edit a local `.env.production` file, `gcloud secrets versions add sailline-frontend-env --data-file=...`, push any frontend commit (or run the trigger manually) to rebuild against the new version.
- Frontend deploy log: `https://console.cloud.google.com/cloud-build/builds?project=105706282249`. Filter on trigger `sailline-frontend-deploy`.
- Manual run: `gcloud builds triggers run sailline-frontend-deploy --branch=main`.
- `firebase-tools` is installed globally during step 4 of the build (~20s). If build minutes ever become a concern, switch to a pre-baked image or move the install to step 1 in parallel with `npm ci`.

## Where things stand at end of session

- Production frontend: `https://sailline.web.app` — auto-deployed on every push to `main` with `frontend/**` changes (after path-filter cleanup)
- Production API: `https://sailline-api-105706282249.us-central1.run.app` — also reachable as `https://sailline.web.app/api/**`
- CORS: eliminated for production traffic
- Outstanding bug count: 0
- Outstanding TODOs: path filters on both triggers, `.env.example` cleanup, the migration framework still
