# 2026-06-02 — Mobile fixes plan (pre-implementation)

**Approval status:** awaiting Grayson sign-off before any code lands.

User selected Tier A + B + C, `expo prebuild` path, optimistic-reconcile for marks. This doc is the gate.

## Sequencing rationale

Order minimises rework: prebuild first means every screen we add this session is built locally (no EAS spend). Bug fixes second so the recording path is honest before we start layering new UI on top. New surface area last so we don't paper over still-broken hooks.

1. **C1** — `expo prebuild` migration. Locks in local Gradle builds.
2. **A1–A4** — recording-screen bugs (marks reset, dup LIVE, double-start, missing speed/heading).
3. **B1** — drawer nav + Settings/Boats/Profile screens.
4. **B2** — auto-pass toggle wiring.
5. **B3** — additional map layers (actual route + wave overlay).

Single commit per item with the file paths called out before the change. No bundling.

---

## C1. `expo prebuild` migration

**Goal:** kill EAS credit burn; build and install locally over USB.

**Steps:**

1. Verify Java 17 + Android Studio + `ANDROID_HOME` + `platform-tools` on PATH (your runbook covers this).
2. From `mobile/`: `npx expo prebuild --platform android --clean`. Generates `mobile/android/` natively.
3. Copy `EAS_BUILD_PROFILE` env handling from `eas.json` into `mobile/android/app/build.gradle` as flavor configs (debug vs release).
4. Stash the Transistorsoft license key handling. Currently lives in `eas secret`. Two options to discuss:
   - `gradle.properties` (gitignored). Simple, you set once locally.
   - `keystore.properties` pattern. Same idea, slightly cleaner.
5. `adb devices` → verify phone visible.
6. `npm start` (Metro) + `npx react-native run-android` for dev iteration.
7. Release builds: `cd android && .\gradlew assembleRelease` then `adb install android/app/build/outputs/apk/release/app-release.apk`.

**Risks flagged:**

- `expo-router` works fine in prebuild mode; verified via Expo docs. Not a blocker.
- `@rnmapbox/maps` needs a `MAPBOX_DOWNLOADS_TOKEN` in `~/.gradle/gradle.properties` for the SDK download. One-time setup.
- Transistorsoft adds `~10MB` to the APK; expected.
- Prebuild **regenerates `/android`** on every run. Custom changes go in `app.config.js` or `expo-build-properties` plugin, not directly in `android/`. Otherwise they get clobbered.

**Tech debt accepted:** Once `android/` is committed, we own native config. Versioned upgrades require running prebuild again and reconciling. Worth it for the credit savings.

**Files changed:** `mobile/package.json` (drop `eas-cli` dev dep), `mobile/app.config.js` (drop EAS-specific config), `mobile/android/` (new tree). Delete `mobile/eas.json`.

---

## A1. Mark-pass UI reset

**Symptom:** Manual pass shows green, then reverts within seconds.

**Root cause:** `useMarkPasses.fetchOnce` runs every 15s and unconditionally overwrites local `passes` state with `race.mark_passes`. Two failure modes:
- (a) Cloud Run instance read-after-write race: poll hits an instance that hasn't seen the mutation yet → returns `[]` → wipes UI.
- (b) `race.mark_passes` arrives wire-encoded as a **JSON string of a JSON string** (visible in `studio_results_20260602_1741.json`). If the mobile parser drops it as `[]` on shape mismatch, every poll wipes the UI.

**Fix (optimistic + reconcile, per your choice):**

1. In `useMarkPasses`, change the polling reducer from "replace" to "union". A pass that's locally known stays known; a pass the server adds is appended.
2. Add `getRace` response sanity check: if `mark_passes` is a string, JSON.parse it before treating it as the array. Log a single warn so we catch the backend bug too.
3. Backend follow-up (out of scope this commit, flagged for next session): why is `mark_passes` returning double-stringified? Check `backend/app/routers/races.py` serialization.

**Files changed:** `mobile/src/hooks/useMarkPasses.ts`, `mobile/src/api/races.ts`.

---

## A2. Duplicate LIVE pill

**Symptom:** Two "LIVE" pills at top of recording screen.

**Root cause:** `UploadStatusBadge` shows label `"LIVE"` when status is `"live"` (`UploadStatusBadge.tsx:54`). Sits next to the LIVE chip in `recording.tsx:216–254`.

**Fix:** Two changes, take whichever you prefer (recommend the first):

1. Change "live" badge label to a coloured dot only (no text), or `"UP"` (uploading), or hide the badge entirely when status is `"live"` — the LIVE chip already signals that.
2. Or: remove the standalone LIVE chip and let `UploadStatusBadge` be the single recording-state indicator.

**Recommendation:** option 1, hide badge when status is `"live"`. Keeps the LIVE chip dominant. Badge only appears when something is wrong (BUFFER / STALL / OFFLINE).

**Files changed:** `mobile/app/(app)/recording.tsx` (conditional render).

---

## A3. "Waiting for previous start action to complete"

**Symptom:** Red error text at bottom of recording screen.

**Root cause:** Transistorsoft's native plugin returns this error when `start()` is called twice. Likely path: `useAutoStartRecorder` fires near `start_at` while user also tapped Start — both call `recorder.start()`. The first start is mid-flight; second one errors.

**Fix:**

1. In `useTrackRecorder.start`, the idempotency check `if (watcherRef.current || watcherPromiseRef.current) return;` exists but doesn't prevent the Transistorsoft-level race when both calls await in parallel.
2. Add a `startingRef` boolean that flips true synchronously before `await startWatcher`, blocking re-entry.
3. Clear the sticky `error` state after the first successful position fix arrives (currently `setError(null)` only happens on flush success — but if uploads stall, the user is stuck staring at the old start error forever).

**Files changed:** `mobile/src/recorder/useTrackRecorder.ts`.

---

## A4. Missing Distance / Speed / Track values

**Symptom:** Guidance card shows "—" for all three.

**Likely cause:** You were stationary on a couch — Transistorsoft `speed_kts` is `-1` (sentinel) when stopped, and the wire format converts `-1` → `null`. `GuidanceCard` renders `speedKt != null ? ... : "—"`, so null → dash.

**Verification needed before fix:**

1. Add a one-line dev console log in `useTrackRecorder.onPosition` to print `point.speed_kts` and `point.heading_deg` from a real Transistorsoft fix.
2. If they're populated when moving but `null` when still: that's correct behaviour, just need a "GPS waiting / stationary" UI hint.
3. If they're `null` even when moving: bug in `backgroundGeolocation.ts` mapping.

**Don't ship a fix until verified on water.** Otherwise we're chasing a non-bug.

**Files touched if verification confirms a bug:** `mobile/src/recorder/backgroundGeolocation.ts`.

---

## B1. Drawer navigation + Settings / Boats / Profile screens

**Webapp parity items:**

- **Race Setup** — already exists (`/race-edit`). Drawer needs an entry point.
- **Boats** — list + create + edit. Mirror webapp `/boats`. Backend API exists (`mobile/src/api/boats.ts` has `listBoats` already).
- **Settings** — app-level prefs: units (kts/mph), default boat, debug screen access, auto-pass toggle (see B2).
- **Profile** — user fields (sail club, home port, etc.). Mirror what's in webapp Settings today.

**Navigation pattern:** `expo-router` Drawer layout. Replace the current `Stack` in `(app)/_layout.tsx` with a Drawer that hosts the map, with the other screens as Drawer routes.

**New files:**

- `mobile/app/(app)/_layout.tsx` — Drawer instead of Stack.
- `mobile/app/(app)/boats.tsx`, `mobile/app/(app)/boat-edit.tsx`.
- `mobile/app/(app)/settings.tsx`.
- `mobile/app/(app)/profile.tsx`.
- `mobile/src/screens/BoatsScreen.tsx`, `BoatEditScreen.tsx`, `SettingsScreen.tsx`, `ProfileScreen.tsx`.
- `mobile/src/api/profile.ts` (if not already).

**Question for you:** the webapp's current "Settings" page conflates user profile and app settings. You flagged this. Plan is to split: **Profile = "who I am"** (name, club, default boat, units in your preferred display). **Settings = "how the app behaves"** (debug screen toggle, auto-pass toggle, theme, notifications). Confirm before I build the screens.

---

## B2. Auto-pass toggle

**Symptom:** No way to disable auto-mark-detection.

**Plan:**

1. New setting in `SettingsScreen`: `auto_mark_pass_enabled` (default ON). Persists in AsyncStorage.
2. New hook `useAutoPassSetting` (mirror `useAutoRouteSetting`).
3. `recording.tsx` reads it. If OFF: skip `useMissedMarkNotifier`, and disable the polling-based detection display. Mark pills still tappable for manual passes.
4. Backend detection still runs — this is a CLIENT visibility toggle, not a backend kill. (Otherwise we'd be deploying backend changes for a UI flag.)

**Files changed:** `SettingsScreen.tsx` (new), `mobile/src/hooks/useAutoPassSetting.ts` (new), `mobile/app/(app)/recording.tsx`.

**Future scope (flagged):** if you want the server to actually stop auto-detecting, that's a per-race flag on the race row, not a user pref. Out of scope today.

---

## B3. Map layers

**Existing:** base map (Mapbox), wind barbs (toggleable via `windOn` state), race marks (auto-drawn from `selectedRace.marks`), computed route (drawn from `routing.route`).

**Adding:**

- **Actual route polyline (recording).** New `ActualRouteLayer` component subscribed to `recorder.points`. Renders a continuous line as fixes arrive. Always visible while recording, hidden otherwise. Performance: cap to last N points (say 5000) to keep render cost bounded; thin via Douglas-Peucker at low zooms.
- **Wave overlay.** Backend `/api/weather?source=...` — need to check what wave data sources exist. If GFS provides wave height grids, mirror the wind-barb pattern. If not, B3-wave gets deferred to a separate session (need to extend backend first).

**Layers FAB UX:** Currently `MapFabs` has one toggle for wind. Need to expand to a layers picker (modal or sub-menu) with checkboxes: Wind / Wave / Actual Track / Computed Route / Marks.

**Files changed:**
- `mobile/src/components/ActualRouteLayer.tsx` (new).
- `mobile/src/components/MapCanvas.tsx` (mount the new layer).
- `mobile/src/components/MapFabs.tsx` + new `LayerPickerModal.tsx` for the picker.
- `mobile/src/components/WindBarbLayer.tsx` (unchanged, but state moves to a layers object).

**Question:** wave layer — do you have a specific data source in mind? NOAA GFS WW3 has SWH (significant wave height). Backend would need a new `source=ww3` weather endpoint. Sizable backend work. **Recommend deferring wave to its own session; ship the actual-track layer this session.**

---

## Order of execution (each its own commit)

1. C1 — prebuild migration. **Stop here and validate a local Gradle build installs and runs before anything else.** This is the gate.
2. A1 — mark-pass reset fix.
3. A2 — dup LIVE pill.
4. A3 — double-start guard + error clearing.
5. A4 — speed/heading verification (no fix yet, just logging).
6. B1 — drawer + Settings/Boats/Profile screens.
7. B2 — auto-pass toggle.
8. B3 — actual-route layer (wave deferred).

After each commit I'll state the file paths so you can verify. No skipping ahead. If anything breaks the local build I stop and we debug before moving on.

---

## What's out of scope today (flagged, not done)

- Backend `race.mark_passes` double-stringification fix (A1 follow-up).
- Wave layer (needs backend weather source first).
- Server-side auto-pass disable flag (per-race, separate scope).
- Webapp Settings split into Profile vs Settings.
- Anything not listed above.

---

## Locked decisions (2026-06-02, Grayson)

1. **Transistorsoft license** → `keystore.properties` (gitignored, lives with release signing config). One place for all don't-commit secrets.
2. **Mapbox tokens** → user has both. `MAPBOX_DOWNLOADS_TOKEN` goes in `~/.gradle/gradle.properties` (user-global). Public `MAPBOX_ACCESS_TOKEN` stays in `app.config.js` env.
3. **Profile vs Settings** → confirmed. Profile = identity (name, club, home port, default boat, preferred units display). Settings = behaviour (auto-pass toggle, theme, debug screen access, notifications).
4. **Wave overlay** → deferred to its own session but stays in the plan. Backend `ww3` weather source TBD.
5. **Settings persistence** → backend sync via JSONB `app_settings` column on `user_profiles`. **Implies new migration `0020_user_app_settings.py` + `GET`/`PUT /api/users/me/settings` endpoints.** Migration is manual per CLAUDE.md — I write it, Grayson applies during deploy. AsyncStorage cache for offline read at launch.

## Starting work

C1 first. Stops at "local Gradle build installs on phone and app launches" before any other commit.
