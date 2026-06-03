# 2026-06-03 — Mobile fixes batch (A1–A4, B1–B3, Item 5)

## What we worked on

Knocked out the full punch-list Grayson raised on 2026-06-02 ahead of a
deployment window. The 2026-06-02 plan document
(`2026-06-02_session_mobile-fixes-plan.md`) stays the canonical spec
for each item; this doc records what actually landed and where it
deviated from the plan.

Backend untouched — every change is in `mobile/`.

## Items shipped

### A1 — mark-pass UI no longer resets (manual taps stick)

- `mobile/src/hooks/useMarkPasses.ts`
  - `fetchOnce` now union-merges the polled server list against local
    state via a new exported `mergePasses(local, server)` helper.
    Server still wins on overlap; locally-known passes survive a
    stale poll. Tracks a `passesRef` so the merge sees the latest
    local baseline without forcing the polling effect to re-mount on
    every render.
  - `markManualPass` keeps its authoritative-replace behaviour and
    re-seeds `passesRef` so the next merge baseline is correct.
- `mobile/src/api/races.ts`
  - New `normaliseRace()` runs all GET / POST / PATCH race responses
    through a `coerceMarkPassArray()` shim that JSON.parses
    double-encoded `mark_passes` strings. One-shot `console.warn` if
    the bug ever fires.

### A2 — duplicate LIVE pill removed

- `mobile/app/(app)/recording.tsx` — `UploadStatusBadge` now only
  renders when `recorder.uploadStatus !== "live"`. The dedicated LIVE
  chip is the single LIVE indicator; the badge re-appears for
  BUFFER / STALL / OFFLINE only.

### A3 — "Waiting for previous start action…" sticky error

- `mobile/src/recorder/useTrackRecorder.ts`
  - New `startingRef` synchronous re-entry guard flips true at the top
    of `start()` before the first await. Blocks the auto-start timer
    racing the user's Start tap. Cleared at the end of `start()` and
    defensively at the top of `stop()`.
  - New `haveLivePositionRef` clears any sticky start-time error on
    the first successful position fix, so a transient
    "Waiting for…" warning doesn't haunt the recording screen for
    the rest of the race.

### A4 — speed / heading verification + waiting hint

- `mobile/src/recorder/backgroundGeolocation.ts` — `normalizeLocation`
  logs one sample per ~10 fixes (dev only) with the raw and mapped
  `speed`, `heading`, `accuracy`. Single short on-water test should
  tell us whether the values are coming through null because
  Transistorsoft never populates them, or because the boat is
  stationary.
- `mobile/src/components/GuidanceCard.tsx` — new `awaitingGps` prop
  swaps the bare em-dashes for "GPS…" microcopy when the recorder is
  live but lacks a fix.
- `mobile/app/(app)/recording.tsx` — wires `awaitingGps` from the
  recorder state.

### B2 — auto-pass toggle (global pref)

- `mobile/src/hooks/useAutoPassSetting.ts` — new global
  (NOT per-race) AsyncStorage-backed boolean, default ON. Mirrors the
  storage pattern of `useAutoRouteSetting` but at module scope.
- `mobile/src/hooks/useMissedMarkNotifier.ts` — added `enabled` option
  (default true for back-compat). Hook still mounts (rules of hooks)
  but short-circuits the arming + alert path when false. Reset effect
  also keys off `enabled` so toggling re-arms from a clean state.
- `mobile/app/(app)/recording.tsx` — reads
  `useAutoPassSetting().enabled` and passes it through.
- Setting surface lives in the new SettingsScreen (see B1).

### B3 — Actual route layer

- `mobile/src/components/ActualRouteLayer.tsx` (new) — renders
  `recorder.points` as a Mapbox LineString casing + stroke. Caps at
  5000 points (keeps the geometry small on a 3-hour race);
  Douglas-Peucker thinning at low zooms flagged as tech debt.
- `mobile/src/components/MapFabs.tsx` — new optional
  `onToggleActualRoute` / `actualRouteOn` props add a footsteps FAB
  to the cluster. Only renders when a handler is wired (i.e., on
  /recording).
- `mobile/app/(app)/recording.tsx` — wires the toggle + mounts
  `<ActualRouteLayer>` inside MapCanvas (above the wind barbs).
- Wave overlay was deferred per the 2026-06-02 plan — needs a backend
  ww3 weather source first.

### Item 5 — duplicate Start FAB removed

- `mobile/src/components/MapActionFabs.tsx` — Start FAB removed. The
  cluster now holds only the Minimize FAB (sheet-expanded-only). The
  in-sheet Start CTA is the single entry point into recording mode,
  matching the precedent set when the Directions FAB was removed on
  2026-05-29 (evening).
- `mobile/app/(app)/index.tsx` — call site no longer passes `onStart`
  or `recording`.

### B1 — drawer parity (Profile / Settings / Boats / Race Setup)

**Deviation from the 2026-06-02 plan, by design.** The plan called
for an `expo-router` Drawer (`@react-navigation/drawer`). Adding that
dependency means installing native modules + re-running `expo prebuild`
+ a fresh Gradle build — non-trivial right before a deployment, and
we just stabilised local builds in C1. This commit ships the same
navigation SURFACE as a Gorhom bottom-sheet menu (Gorhom is already
a dep, zero native config touched). Routes are real expo-router stack
routes, so a future commit can swap the launcher for a real Drawer
without touching the screens.

- `mobile/src/components/AppMenuSheet.tsx` (new) — bottom-sheet menu
  with: Races, New race, Boats, Settings, Profile, Sign out. Forwarded
  ref exposes `open()` / `close()`.
- `mobile/app/(app)/index.tsx` — hamburger FAB top-left + mounts
  `AppMenuSheet` at index=-1. Inside SafeAreaView so it clears the
  notch.
- `mobile/src/screens/SettingsScreen.tsx` (new) — auto-pass toggle
  (B2 surface) + link into the existing recorder-debug screen.
  Header with back chevron. Footer copy notes more settings coming.
- `mobile/src/screens/ProfileScreen.tsx` (new) — identity card
  (Google avatar + display name + email) + Sign out button. Footer
  notes sail-club / home-port parity coming.
- `mobile/src/screens/BoatsScreen.tsx` (new) — read-only list using
  the existing `listBoats()` API. Pull-to-refresh. Header copy notes
  "edit on web" until a follow-up wraps POST/PATCH on `/api/boats`.
- `mobile/app/(app)/settings.tsx`, `profile.tsx`, `boats.tsx` (new) —
  thin route shims that re-export the screens from `src/screens/`.

## Decisions made and rationale

- **Union-merge instead of last-write-wins on `useMarkPasses`.** The
  user's video showed the manual pass reverting within seconds, which
  the poll's unconditional replace explains exactly. Union with
  server-wins-on-overlap keeps the eventual-consistency model honest
  while making the UI durable to a stale read.
- **Defensive coerce on `mark_passes` in the API layer, not the hook.**
  Putting it in `api/races.ts` means every consumer (current and
  future) gets the same shape, and the bug only logs once globally.
- **`startingRef` synchronous boolean, not a Promise.** A Promise
  would let the second caller `await` and proceed; we want the
  second caller to fail fast and return, not queue behind the first.
- **`enabled` on `useMissedMarkNotifier`, not a wrapper hook.** Rules
  of hooks forbid conditional mounting; passing a flag through keeps
  the call site identical and lets the hook reset its state cleanly
  on toggle.
- **Bottom-sheet menu in lieu of a real Drawer.** Gorhom is already
  in the dep tree. Adding `@react-navigation/drawer` is a native
  config change requiring another prebuild + APK rebuild + Gradle
  cycle. The user prioritised "before the deployment," and the menu
  surface (Races / Boats / Settings / Profile / Sign out) is the
  same regardless of launcher. Tech debt flagged below.
- **Start FAB removed, not relocated.** The 2026-06-02 plan was
  silent on item 5 (FAB position). The screenshots showed two Start
  FABs because the in-sheet CTA and the MapActionFabs FAB rendered
  simultaneously. Removing the MapActionFabs one matches the
  precedent set when the Directions FAB was removed for duplicating
  the in-sheet Recompute button.
- **Sampled `normalizeLocation` log, not a UI display.** A4 was
  explicitly a verification step in the plan, not a fix. The log
  gives us the data to decide whether a real fix is needed without
  shipping a speculative one.
- **`awaitingGps` UI hint is a small cosmetic improvement.** Replaces
  bare em-dashes during the cold-start window with "GPS…" so the
  user knows the recorder is alive while waiting for the first
  speed-carrying fix.

## Files changed

New:
- `mobile/src/hooks/useAutoPassSetting.ts`
- `mobile/src/components/ActualRouteLayer.tsx`
- `mobile/src/components/AppMenuSheet.tsx`
- `mobile/src/screens/SettingsScreen.tsx`
- `mobile/src/screens/ProfileScreen.tsx`
- `mobile/src/screens/BoatsScreen.tsx`
- `mobile/app/(app)/settings.tsx`
- `mobile/app/(app)/profile.tsx`
- `mobile/app/(app)/boats.tsx`

Modified:
- `mobile/src/hooks/useMarkPasses.ts`
- `mobile/src/hooks/useMissedMarkNotifier.ts`
- `mobile/src/api/races.ts`
- `mobile/src/recorder/useTrackRecorder.ts`
- `mobile/src/recorder/backgroundGeolocation.ts`
- `mobile/src/components/UploadStatusBadge.tsx` (no change — kept as-is)
- `mobile/src/components/GuidanceCard.tsx`
- `mobile/src/components/MapFabs.tsx`
- `mobile/src/components/MapActionFabs.tsx`
- `mobile/app/(app)/index.tsx`
- `mobile/app/(app)/recording.tsx`

## Verification

- **TypeScript check needs to run on Windows.** The bash mount of
  `E:\` in this sandbox truncates files (memory note
  `feedback_bash_mount_unreliable`); `npx tsc --noEmit` here surfaces
  spurious "missing closing tag" errors because tsc reads the same
  truncated views. Real files on disk are correct (confirmed via the
  Read tool). Grayson: run `cd mobile; npx tsc --noEmit` from
  PowerShell before committing.
- Mobile has no test suite yet (vitest / jest unconfigured). Manual
  smoke after the next local Gradle build:
  1. Tap hamburger → menu opens → Settings opens → auto-pass toggle
     persists across kill/relaunch.
  2. Tap hamburger → Boats opens → list renders (or empty state
     copy renders).
  3. Tap hamburger → Profile opens → name + email render → Sign out
     works.
  4. Start a recording → LIVE chip alone (no duplicate badge) → walk
     in circles → actual-track polyline draws → footsteps FAB
     toggles it on/off → Stop.
  5. Manually pass a mark → confirm pill stays green for ≥ 60 s
     (covers two poll cycles).
  6. Settings → flip auto-pass OFF → start a race in a Couch test
     → no missed-mark notification fires regardless of position.

## Open items / next steps

- **Verify A4 on water.** With the sampled log in place, one short
  on-water session will tell us whether speed/heading are truly null
  from the SDK or just suppressed correctly while stationary. Decide
  whether a real fix is needed.
- **Boat edit on mobile.** BoatsScreen is read-only today. Wrap the
  existing backend POST/PATCH on `/api/boats` and add a real
  `BoatEditScreen`.
- **Profile parity.** Sail club, home port, default boat, units —
  backend already has the columns, mobile just needs editable fields.
- **Real Drawer.** Swap `AppMenuSheet` for `expo-router/drawer` next
  time we're doing a prebuild anyway.
- **Settings expansion.** Theme switcher, units, default boat,
  feature-flag toggles for the recorder debug paths.
- **Backend follow-up — `mark_passes` double-stringification.** The
  defensive coerce in `api/races.ts` is a mobile-side shim. The
  real fix lives in `backend/app/routers/races.py`; flagged when A1
  was done.

## Technical debt flagged

- **`AppMenuSheet` vs real Drawer.** Documented above. Same nav
  surface, different launcher. The screens themselves are real
  expo-router routes — the swap is purely the entry-point UI.
- **`ActualRouteLayer` Douglas-Peucker.** Today we cap at 5000 points
  and drop the oldest. A 3-hour race at 1 Hz will lose the first
  ~7000 fixes from the displayed line. Real thinning at low zooms is
  the right answer.
- **No mobile test infra.** `mergePasses`, `coerceMarkPassArray`,
  `normalizeLocation`, `useAutoPassSetting` are all pure functions
  worth unit tests. Setting up vitest or jest in the mobile workspace
  is its own scope.
- **TypeScript verification gated on Windows.** Bash mount of `E:\`
  in this sandbox is unreliable for tsc / vitest reads. Build-time
  checks must run from PowerShell. Memory note already captures this.
- **Spurious tsc errors masked the verification step in-session.**
  No tsc-clean confirmation was possible here; landing relied on
  careful Read-tool round-trips. Worth standing up a CI check (GitHub
  Actions or a Cloud Build trigger) that runs `npx tsc --noEmit` on
  PRs to mobile so we don't carry this risk forward.
- **`backgroundGeolocation` dev log uses `console.log`.** It is
  `__DEV__`-gated so production builds drop it, but if Metro is
  configured to keep console in release builds at some point this
  would surface. Worth migrating to the on-device `recorderLog` ring
  buffer next time we touch this file.
