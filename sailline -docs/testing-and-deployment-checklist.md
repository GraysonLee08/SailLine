# SailLine — Testing & Deployment Checklist

Reference runbook for shipping changes safely. All commands are **PowerShell**
(no backslash line-continuations — use backtick `` ` `` or single lines).

**Repo root:** `E:\Personal\Coding\SailLine`
**Prod API:** `https://sailline-api-105706282249.us-central1.run.app`

**Golden rules**

- CI runs tests *before* deploy on every push to `main` — a failing test blocks the deploy.
- **Backend migrations are manual** and decoupled from auto-deploy. Apply them deliberately (see Backend → Deployment).
- Before assuming a deploy finished, check for an in-flight build:
  ```powershell
  gcloud builds list --limit=1 --ongoing
  ```

---

## 1. Backend (FastAPI → Cloud Run)

### 1.1 Testing

- [ ] `cd E:\Personal\Coding\SailLine\backend`
- [ ] Activate the venv: `.\.venv\Scripts\Activate.ps1`
- [ ] (First run / deps changed) `pip install -r requirements.txt`
- [ ] Fast suite (matches what CI runs): `pytest -m "not slow"`
- [ ] Full suite incl. network/NOAA smoke tests (optional, slower): `pytest`
- [ ] Single file while iterating: `pytest tests/test_isochrone_engine.py -v`
- [ ] Single test: `pytest tests/test_navigability.py::test_name -v`
- [ ] (Optional) Run the API locally and sanity-check `/docs`:
  ```powershell
  uvicorn app.main:app --reload --port 8080
  ```
- [ ] Confirm **green** before pushing — a red `pytest -m "not slow"` will block the Cloud Build deploy.

> Notes: `pytest.ini` sets `pythonpath=.` and `asyncio_mode = auto` (async tests need no `@pytest.mark.asyncio`).

### 1.2 Deployment

Auto-deploys on push to `main` via `infra/cloudbuild.yaml` (→ Cloud Run `sailline-api`). **Migrations are the manual part — do them first.**

**If the change has NO migration:**

- [ ] Tests green locally (`pytest -m "not slow"`).
- [ ] `git push` to `main`.
- [ ] Watch the build: `gcloud builds list --limit=1 --ongoing`.
- [ ] Smoke-check prod after the revision goes live (e.g. hit `/health`).

**If the change HAS a migration** (read `docs/migrations.md` first):

- [ ] Verify which DB secret currently works (the app-password / postgres-password name drifts — verify empirically, don't assume).
- [ ] Start the Cloud SQL Auth Proxy to reach prod Cloud SQL.
- [ ] **Additive migration** (new table/column, safe): apply **before** pushing.
  ```powershell
  cd E:\Personal\Coding\SailLine\backend
  alembic current
  alembic upgrade head
  ```
- [ ] **Destructive migration** (drop/rename): split across **two commits/deploys** — deploy code that tolerates both shapes first, migrate, then deploy the cleanup. Never drop in the same deploy that stops using the column.
- [ ] After a destructive migration, force a Cloud Run revision rollover so asyncpg drops stale prepared statements:
  ```powershell
  gcloud run services update sailline-api --region us-central1 `
    --update-env-vars=BUMP=$([int](Get-Date -UFormat %s))
  ```
- [ ] `git push`, then `gcloud builds list --limit=1 --ongoing`.
- [ ] Verify `alembic current` matches the deployed code's expectations.

> Never add tables to `infra/schema.sql` — Alembic owns migrations; `schema.sql` is one-time bootstrap only.

---

## 2. Frontend

### 2.1 Web App (React + Vite → Firebase Hosting)

#### 2.1.1 Testing

- [ ] `cd E:\Personal\Coding\SailLine\frontend`
- [ ] (First run / deps changed) `npm install`
- [ ] Unit tests (matches CI): `npm test` (`vitest run`)
- [ ] Watch mode while iterating: `npm run test:watch`
- [ ] Production build sanity check: `npm run build`
- [ ] Local dev against local backend: create `.env.local` with `VITE_API_URL=http://localhost:8080`, then `npm run dev` (→ http://localhost:5173)
- [ ] Confirm **green** before pushing — a failing `npm test` blocks the Cloud Build deploy.

> Note: vitest must be run on Windows — it cannot run in the agent sandbox (bus error regardless of test contents).

#### 2.1.2 Deployment

Auto-deploys on push to `main` via `infra/cloudbuild.frontend.yaml` (→ Firebase Hosting), which runs `npm test` before deploy.

- [ ] Tests green locally.
- [ ] `git push` to `main`.
- [ ] Watch the build: `gcloud builds list --limit=1 --ongoing`.
- [ ] Verify the live site (https://sailline.web.app) and that `/api/**` calls resolve (same-origin rewrite → Cloud Run).
- [ ] **Manual deploy (fallback only)** if you must bypass CI:
  ```powershell
  cd E:\Personal\Coding\SailLine\frontend
  npm run deploy   # vite build && firebase deploy --only hosting
  ```

### 2.2 Mobile App (Expo / React Native — prebuild + local Gradle, Android)

> Not EAS. Builds run locally via Gradle and install over USB. There is **no automated test harness yet** (tracked tech debt) — verification is on-device.

#### 2.2.1 Testing

**Fast path — JS/TS-only changes (no new native modules):** no rebuild needed; reload the existing dev-client build.

- [ ] `cd E:\Personal\Coding\SailLine\mobile`
- [ ] Phone connected, dev-client app installed (NOT Expo Go).
- [ ] Start Metro: `npx expo start --dev-client` (or `npm start`)
- [ ] Backend defaults to **prod**. For a local backend, set `EXPO_PUBLIC_API_URL=http://<LAN-ip>:8080` first.
- [ ] Reload JS on device (shake → Reload, or `r` in Metro) and run the on-device smoke test for the feature touched.
- [ ] If Metro won't connect (`failed to connect to :8081`): it's Windows Firewall — use `--tunnel`, add a firewall rule, or set the network profile to Private.

**Full path — new native deps / config / plugin changes:** rebuild + install the dev client.

- [ ] `adb devices` shows the phone as `device` (accept the USB-debug prompt if `unauthorized`).
- [ ] `npx expo run:android` (prebuild + assembleDebug + install + Metro in one shot), then iterate with `npm start`.
- [ ] Run the on-device smoke test.

#### 2.2.2 Deployment (build a shareable / release APK + sideload)

One-time prerequisites:

- [ ] JDK 17 (`java -version` → 17.x), Node 20+, `adb` on PATH, `ANDROID_HOME` set.
- [ ] `~/.gradle/gradle.properties` contains `MAPBOX_DOWNLOADS_TOKEN` (secret `sk.` token) and `TRANSISTOR_LICENSE` (never commit these).
- [ ] `mobile/.env.local` contains `EXPO_PUBLIC_MAPBOX_TOKEN` (public `pk.` token) and `EXPO_PUBLIC_API_URL`.

Build steps:

- [ ] Remove `expo-updates` before a release build (its `:app:createReleaseUpdatesResources` task fails the build in this monorepo):
  ```powershell
  npm ls expo-updates
  npm uninstall expo-updates expo-eas-client expo-manifests expo-structured-headers expo-updates-interface
  ```
- [ ] Regenerate the native tree:
  ```powershell
  cd E:\Personal\Coding\SailLine\mobile
  npx expo prebuild --platform android --clean
  ```
- [ ] Build the release APK:
  ```powershell
  cd E:\Personal\Coding\SailLine\mobile\android
  .\gradlew assembleRelease
  ```
  (First release build needs a signing keystore; debug builds don't.)
- [ ] Install on the connected phone:
  ```powershell
  adb install -r E:\Personal\Coding\SailLine\mobile\android\app\build\outputs\apk\release\app-release.apk
  ```
- [ ] Re-run the on-device smoke test on the release build.
- [ ] Commit the generated `android/` tree if it changed:
  ```powershell
  cd E:\Personal\Coding\SailLine
  git add mobile/android mobile/.gitignore mobile/app.config.js
  git commit -m "chore(mobile): rebuild native tree"
  ```

> Mobile changes do **not** trigger any Cloud Build pipeline — only `backend/` and `frontend/` auto-deploy. Pushing mobile code just version-controls it; distribution is the APK above. Common build failures (Mapbox `401`, `tslocationmanager` not found, runtime `MAPBOX_ACCESS_TOKEN not set`) are covered in `2026-06-02_C1-prebuild-runbook.md`.

---

## Quick command index

| Task | Command |
|------|---------|
| Backend fast tests | `pytest -m "not slow"` (in `backend/`, venv active) |
| Backend local run | `uvicorn app.main:app --reload --port 8080` |
| Apply migration | `alembic upgrade head` (in `backend/`, via Cloud SQL proxy) |
| Web tests | `npm test` (in `frontend/`) |
| Web manual deploy | `npm run deploy` (in `frontend/`) |
| Mobile JS iterate | `npx expo start --dev-client` (in `mobile/`) |
| Mobile build+install (debug) | `npx expo run:android` (in `mobile/`) |
| Mobile release APK | `.\gradlew assembleRelease` (in `mobile/android/`) |
| Check in-flight deploy | `gcloud builds list --limit=1 --ongoing` |
