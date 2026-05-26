# Phase 2 roadmap — split into 2a (shipped overnight) + 2b (later)

## TL;DR

The original Phase 2 plan in `2026-05-24_mobile-development-plan.md` bundles "mobile management parity" (boats + crew + race creation) with "mobile race picker + recording UI" into one M-sized phase. Tonight's deadline (functional Android app before next live race test) made me split:

- **Phase 2a — race-day Android minimum** (shipped overnight, awaiting your review).
- **Phase 2b — management parity on mobile** (deferred; web continues to handle these).

The split is justifiable because the race-day workflow doesn't need on-device race creation, boat editing, or crew management — those are done on the web ahead of time. The phone just needs to *list* your races and *record* the one you tap.

---

## Phase 2a — what shipped overnight

### Goal
A signed-in user opens the Android app, sees their list of races, taps one, hits Start (or has it auto-start 5 min before gun), races with screen locked, hits Stop. End-to-end on device, no UUID-pasting.

### Files added
- `mobile/src/types.ts` — typed `Race`, `RaceMark`, `MarkPass`.
- `mobile/src/api/races.ts` — `listRaces()`, `getRace(id)`.
- `mobile/src/lib/formatRaceDate.ts` — pure date formatter ("Sat 30 May · 14:00").
- `mobile/src/recorder/useAutoStartRecorder.ts` — TS port of the web hook (auto-start at `start_at - 5min`).
- `mobile/src/screens/RacePickerScreen.tsx` — FlatList of races, pull-to-refresh, tap-to-select, "Raced" pill.
- `mobile/src/screens/RecorderScreen.tsx` — extracted recorder UI, back button (disabled while recording), auto-start banner, GPS quality stats.

### Files modified
- `mobile/App.tsx` — auth gate + AuthedShell state machine. Recorder hook now lives in AuthedShell (lifetime = signed-in session, not screen).

### What I deliberately did NOT change
- **No new native modules.** No `app.config.js` change, no `eas.json` change.
- **No new npm deps.** Everything uses what was already installed for Phase 1.
- **Consequence: no EAS rebuild needed.** Existing dev client picks up changes via Metro fast-refresh (`npx expo start --dev-client`). That keeps the morning iteration loop fast.

### What you do in the morning
1. `cd mobile && npx expo start --dev-client` (no `npm install` needed — no new deps).
2. Open the dev client on your Android phone.
3. Sign in → see your races → tap one → see the recorder.
4. Eyeball the UI. If it's ugly or the wrong info is shown, file feedback or revert: `git checkout -- mobile/App.tsx && git rm mobile/src/screens mobile/src/api mobile/src/lib mobile/src/types.ts mobile/src/recorder/useAutoStartRecorder.ts`.
5. Smoke-test recording: start, lock screen, move for a few minutes, stop. Should behave identically to the Phase 1 harness because the recorder hook is unchanged.

### Known limitations of 2a (intentional)
- **No race creation on mobile.** Plan races on the web. Mobile is read-only for the race list.
- **No race edit.** Same — edit on the web. (Marks, start time, boat class, etc.)
- **No boat or crew management on mobile.** Web-only for now.
- **Auto-start banner doesn't tick down live.** It shows the countdown at the moment of arming; the precise live countdown would need a per-second re-render and isn't worth it for an MVP banner.
- **No filter/sort/search on the race list.** Add later if you accumulate more than ~20 races.
- **No SSE "better-route" notifications.** That's Phase 3 (routing on mobile).
- **No map rendering, wind barbs, or routing.** Also Phase 3.

---

## Phase 2b — deferred

Originally Phase 2 also called for: boats CRUD, crew CRUD, race creation, all on mobile. Defer the entire bundle behind 2a because:

1. **Race-day doesn't need it.** You create races on the web, on a real keyboard, with a Mapbox click-to-drop-mark UX that doesn't translate to a phone screen.
2. **Forms on mobile are a substantial UI build.** Boat edit alone has ~12 fields incl. PHRF cert upload; crew invitation has email + role. Native form patterns differ enough from React JSX that it's a port-not-rewrite kind of job.
3. **Navigation library decision is real overhead.** A second screen pair (RacePicker, Recorder) is fine as a single-file state machine in `App.tsx`. Once we have 5+ screens with deep links between them, `expo-router` becomes the right call — but that's a one-time setup cost worth bundling with the 2b build, not paying twice.

### When to do 2b
Trigger: you find yourself wanting to plan or edit a race on the phone, away from a desk. Until then 2b is over-investment.

### What 2b will need (skeleton, for future planning)
- Pick a navigator. **Recommendation: expo-router** for file-based routing and Expo-native ergonomics. (`@react-navigation/native` is the alternative; more flexible, more wiring.)
- Extract shared API client modules from `mobile/src/api/` into `packages/shared/src/api/` and have web import from there. Eliminates URL drift between the two clients.
- Port `useBoats`, `useCrew` hooks from `frontend/src/hooks/` to `packages/shared/src/hooks/` (they're framework-agnostic — pure data fetching).
- Build native form screens: BoatList → BoatEdit, CrewList → CrewInvite, RacesList (extends 2a's picker) → RaceEdit.
- The Mapbox-click-to-drop-mark interaction on RaceEdit is the most novel piece — needs `@rnmapbox/maps` integration (also a Phase 3 prerequisite, so consider sequencing 3 before 2b's RaceEdit specifically).

### Estimated 2b size
M–L. A full week of focused work, not an overnight. Definitely a "plan before paste" item.

---

## Sequencing recommendation post-2a

1. **Now:** test 2a on device. Run a real race with it. Confirm the recorder still hits the "continuous gap-free track" bar from Phase 1, now with the better UX.
2. **Next:** Phase 3 (pre-race routing on mobile) is more valuable than 2b for actually winning races. Brings the route + wind onto the phone.
3. **2b** waits until either you genuinely need it OR Phase 3's Mapbox integration makes RaceEdit cheap to add.

---

## Open question for Grayson

The auto-start hook ports the web behaviour exactly: fires 5 min before gun. For mobile this is timed by JS setTimeout, which keeps running while the app is in the background ONLY while the JS runtime is alive. If the OS kills the JS runtime before gun (more likely the longer you leave the app sitting), auto-start doesn't fire.

**Mitigation options:**
- (Status quo) Treat auto-start as a convenience — user opens the app within ~15 min of gun.
- Schedule a *local notification* at `start_at - 5min` and have tapping it auto-start the recorder. Wakes the JS even if it had been killed. Adds `expo-notifications` as a dep — small, well-supported, but is a new native module → would need an EAS rebuild.
- Use Transistorsoft's `BackgroundFetch.scheduleTask` to run a JS callback at a specific instant. We already have bg-fetch installed; might be free.

Not making this call tonight. Flagging so 2b planning has it in scope.
