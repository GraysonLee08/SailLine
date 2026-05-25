# Scaffolding plan — monorepo + Expo mobile app (2026-05-24)

Companion to the ADR (`2026-05-24-mobile-framework-decision.md`). This is the **structural setup only** — turning the repo into a shared monorepo and standing up an empty-but-runnable Expo app. Feature work is in the development plan. No feature code here.

**Per project rules this is a plan to review before any code is written.** Commands are PowerShell-flavoured (backtick continuations, never `\`).

## End state

```
E:\Personal\Coding\SailLine\
  package.json            # NEW — npm workspaces root
  backend/                # unchanged (Python)
  packages/
    shared/               # NEW — @sailline/shared (TS source of truth)
  frontend/               # existing web app, now consumes @sailline/shared
  mobile/                 # NEW — Expo React Native app
  infra/                  # unchanged (Cloud Build paths still valid)
```

`frontend/` is intentionally **not** renamed to `apps/web`, so `infra/cloudbuild.frontend.yaml` and `firebase.json` paths keep working untouched.

## Guiding constraints
- Don't break the web deploy. The frontend Cloud Build pipeline runs `npm` inside `frontend/`; workspaces must not change how that install resolves. Verify `npm run build` + `npm test` still pass from `frontend/` after each step.
- `packages/shared` is the single JS source of truth — the existing `regions.py` ↔ `regions.js` mirror discipline is promoted to "Python mirrors `packages/shared`."
- Respect `.nvmrc` (pinned Node version) across all workspaces.

## Step 0 — Branch and baseline
1. Create a branch (e.g. `mobile-monorepo-scaffold`).
2. Record green baseline: from `frontend/`, run `npm ci`, `npm run build`, `npm test`. (Vitest can't run in the cowork sandbox — this is a Windows-only check.)

## Step 1 — Introduce npm workspaces at the root
1. Add a root `package.json`:
   ```json
   {
     "name": "sailline",
     "private": true,
     "workspaces": ["packages/*", "frontend", "mobile"]
   }
   ```
2. Move `frontend/`'s lockfile usage to the root: delete `frontend/package-lock.json`, run `npm install` at the root to generate a single root lockfile.
3. **Risk / checkpoint:** confirm `frontend/` still builds with hoisted `node_modules`. If a dependency misresolves, pin it in `frontend/package.json` or use `nohoist` sparingly. Re-run the Step 0 baseline.

## Step 2 — Create `packages/shared`
1. `packages/shared/package.json`: `"name": "@sailline/shared"`, `"private": true`, `"type": "module"`, `"main"`/`"exports"` pointing at the source (TS consumed directly by both bundlers — Vite and Metro transpile TS natively; no separate build step initially).
2. Add a minimal `tsconfig.json` (the repo is currently JS; TS here is additive and gives both apps type safety without forcing a frontend rewrite).
3. **Move** these framework-agnostic modules out of `frontend/src/lib/` into `packages/shared/src/` (convert `.js`→`.ts` opportunistically, or keep `.js` to start): `latlon`, `morfMarks`, `morfCourses`, `regions`, `boatClasses`, `markRounding`, plus the IMU axis logic (`imuAxes`) and any pure telemetry wire-format helpers (e.g. `gpsPointToWire` shape).
4. Leave UI- or browser-coupled code in `frontend/` (anything touching `window`, the DOM, Mapbox GL, or React).

## Step 3 — Point the web app at the shared package
1. Add `"@sailline/shared": "*"` to `frontend/package.json` dependencies; `npm install` at root links it.
2. Replace `frontend/src/lib/...` imports of the moved modules with `@sailline/shared` imports.
3. Update the regions mirror note in `CLAUDE.md`: the JS source now lives in `packages/shared`; `backend/app/regions.py` mirrors it.
4. **Checkpoint:** re-run the Step 0 baseline (build + tests green) before touching mobile.

## Step 4 — Bootstrap the Expo app in `mobile/`
1. `npx create-expo-app@latest mobile --template` (blank TypeScript template).
2. Set `mobile/package.json` name to `@sailline/mobile`, add `"@sailline/shared": "*"`.
3. **Metro monorepo config** (required): add `mobile/metro.config.js` with `watchFolders` including the repo root and `nodeModulesPaths` for both `mobile/node_modules` and root `node_modules`. This is the standard Expo-in-a-monorepo step and the most common source of "module not found" pain — do it now, not later.
4. Smoke test: `npx expo start`, open in Expo Go on a physical phone, confirm a screen that imports something trivial from `@sailline/shared` renders. This proves the shared-package wiring across Metro.

## Step 5 — Backend connectivity skeleton
1. Add an API base-URL config to `mobile/` (env-driven: local `http://<LAN-ip>:8080`, prod the Cloud Run URL). Note: mobile can't use `firebase.json`'s same-origin `/api/**` rewrite, so it calls the Cloud Run origin directly — CORS must allow it (backend config check).
2. Port the auth approach: Firebase Auth via the Expo-compatible SDK; attach the ID token to requests, mirroring the web `apiFetch` pattern. (Wiring only here; full auth UI is development-plan work.)

## Step 6 — Background geolocation + EAS dev client
1. Because Transistorsoft is a native module, Expo Go can't load it — you need a **dev client**. Install the plugin and its Expo config plugin, add it to `app.json`/`app.config.js` plugins with the foreground-service + iOS background-location config.
2. `eas build --profile development --platform android` to get an installable dev client; iOS dev client comes from EAS too (cloud macOS).
3. Defer actual recording logic to the development plan — this step just proves the native module links and the permission prompts appear.

## Step 7 — EAS configuration
1. `eas init` (links the Expo project), add `eas.json` with `development`, `preview`, and `production` build profiles.
2. Configure credentials: Android keystore (EAS-managed is fine) and iOS signing (EAS manages certs/profiles against your Apple Developer account — no Mac needed).
3. Confirm a cloud build succeeds for **both** platforms before any feature work, so the iOS path is proven early rather than discovered broken late.

## Step 8 — Retire Capacitor leftovers
1. Remove `frontend/capacitor.config.ts` and the Capacitor section from `.gitignore`.
2. Decide the fate of `frontend/src/lib/geolocation.js`: its web branch may still be useful to the web app; the native (Capacitor) branch is dead. Either simplify it to web-only or move the watcher abstraction into `mobile/` against the RN plugin.

## Step 9 — CI / docs
1. Verify `infra/cloudbuild.frontend.yaml` still builds (workspace install may need `npm ci` at root or a `--workspace=frontend` flag — adjust if the pipeline assumed a `frontend/`-local lockfile).
2. Decide on a mobile CI later (GitHub Actions + EAS) — flagged as debt, not blocking.
3. Update `CLAUDE.md` with the new layout, the `packages/shared` source-of-truth rule, and mobile dev commands.

## Acceptance criteria for "scaffolding done"
- Root `npm install` links all three workspaces.
- `frontend/` build + tests still green (Windows).
- `@sailline/shared` imported successfully from both web and mobile.
- An EAS **dev-client** build runs on a physical Android device **and** an EAS iOS build completes in the cloud.
- The Transistorsoft permission prompt appears on device (no recording logic yet).

## Known risks
- **Metro monorepo resolution** — the usual failure point; Step 4.3 addresses it up front.
- **Dependency hoisting** breaking the Vite build — caught by the Step 1/3 checkpoints.
- **Cloud Build lockfile assumptions** — Step 9.1.
- **iOS-without-Mac iteration** — builds work via EAS, but no local Simulator; physical-device testing only.
