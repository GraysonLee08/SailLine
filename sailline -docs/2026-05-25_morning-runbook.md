# Morning runbook — verify the overnight monorepo + Expo scaffold

PowerShell, run from `E:\Personal\Coding\SailLine`. Backtick (`` ` ``) for line continuations, never `\`. Run steps in order; **paste back the output of any step that errors** and stop there.

## What landed overnight (all additive — `frontend/` and `backend/` untouched)
- `package.json` (root) — npm workspaces: `packages/*`, `frontend`, `mobile`.
- `packages/shared/` — `@sailline/shared`, the JS source of truth. 8 pure modules copied from `frontend/src/lib` (latlon, regions, boatClasses, morfMarks, morfCourses, markRounding, windBarb, imuAxes) + a barrel `src/index.js`. **Verified bundlable in the sandbox (38 exports, no collisions).**
- `mobile/` — Expo app, owner `GraysonLee08`, slug `sailline`. Monorepo Metro config, `eas.json`, and an `App.tsx` that imports from `@sailline/shared` to prove wiring. **Verified bundlable in the sandbox.** Pinned to **Expo SDK 54** (expo ~54.0.34, react-native 0.81.5, react 19.1.0, expo-status-bar ~3.0.9) to match the Expo Go on your phone — the initial SDK 56 scaffold was newer than any released Expo Go.

The web app was **not** cut over to `@sailline/shared` yet — it still uses its own `frontend/src/lib` copies. That cutover is a supervised step at the end (so we don't break the web build unverified).

## Step 1 — Review the diff
```powershell
git status
```
Expect only **new** files: `package.json`, `packages/`, `mobile/`. Your earlier uncommitted backend work should be unchanged. Consider committing on a branch:
```powershell
git checkout -b mobile-monorepo-scaffold
```

## Step 2 — Switch to a single workspace lockfile
```powershell
Remove-Item .\frontend\package-lock.json -ErrorAction SilentlyContinue
npm install
```
This creates one root `package-lock.json`, hoists deps, and symlinks `@sailline/shared` into both apps. **This is the highest-risk step** (hoisting can occasionally upset a dependency). If it errors, paste the output.

## Step 3 — Verify the web app still builds (regression gate)
```powershell
cd .\frontend
npm run build
npm test
cd ..
```
Both should pass exactly as before. If `npm run build` fails after the workspace change, that's the hoisting risk — paste the error and I'll pin the offending dep. (Vitest is the Windows-only check; it can't run in my sandbox.)

## Step 4 — Run the mobile app in Expo Go (the payoff)
On your phone, install **Expo Go** from the App Store / Play Store, then:
```powershell
cd .\mobile
npx expo start
```
Scan the QR code with Expo Go (Android) or the Camera app (iOS). The app should open to a dark "SailLine mobile — @sailline/shared wiring check" screen showing:
- Base region (Chicago): `conus`
- `parseCoord("41 56.10N")`: a parsed value
- MORF marks loaded: a non-zero count (~24)
- Boat classes loaded: a non-zero count

If you see those values, the monorepo + shared package + Metro resolution all work. **Paste back what the screen shows (or a screenshot).** Expo Go is enough here because there are no native modules yet — Transistorsoft/background-geo needs a dev client, which comes in Phase 1.

If Metro throws "Unable to resolve @sailline/shared", paste it — that points at the Metro/hoisting config and I'll adjust.

## Step 5 — (Optional, when you're ready) link EAS
Only needed before cloud builds; fine to defer.
```powershell
cd .\mobile
npx eas-cli login          # use your GraysonLee08 account
npx eas-cli init           # links the project, writes an EAS projectId
```
**Paste me the `projectId`** it prints — I'll add it to `mobile/app.json` (`extra.eas.projectId`). It's not secret.

## What I'll do once you confirm Step 4
- Cut `frontend/` over to import from `@sailline/shared` and delete the duplicated `frontend/src/lib` copies (you re-run `npm run build` to verify).
- Retire `frontend/capacitor.config.ts` and the Capacitor `.gitignore` section.
- Then start Phase 1 (background GPS) planning.

## Known caveats
- I couldn't run `npm install`/`expo`/`eas` from my side — Linux sandbox vs your Windows repo (wrong native binaries). All commands above run on your machine; that's expected.
- Expo SDK/RN/React versions came from `create-expo-app@latest`; if your install resolves slightly different patch versions, that's fine.
- `@sailline/shared` is plain JS for now; an ambient `mobile/types/shared.d.ts` keeps TypeScript quiet. We can migrate it to typed TS later.
