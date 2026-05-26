# Phase 1 plan — Background GPS capture (2026-05-25)

Implements Phase 1 of `2026-05-24_mobile-development-plan.md`. Builds on the ADR
(`2026-05-24-mobile-framework-decision.md`) and folds in scaffolding Steps 6–7
(EAS dev client + Transistorsoft), which were deferred at scaffold time — the
current `mobile/app.json` has **no** plugins block and `mobile/package.json` has
neither `expo-dev-client` nor the Transistorsoft plugin.

**Per project rules this is a plan to review before any code is written.** No
implementation code lands until you approve. Commands are PowerShell-flavoured
(backtick continuations, never `\`).

The goal of Phase 1, and the entire reason for the pivot: **a screen-locked
phone in motion produces a continuous, gap-free track in `track_points`.** Phase
1 is **GPS only** — IMU/heel capture stays in Phase 4 (it's a HUD feature), and
the existing IMU code in the web recorder is not ported here.

---

## 0. Prerequisites (you, before I write code)

These are the Part 0 items that Phase 1 specifically blocks on:

| # | Item | Why it blocks Phase 1 |
|---|------|----------------------|
| 1 | **Transistorsoft license** purchased (Android at minimum) | Release builds need it. Debug/dev-client works on the trial, so we *can* start, but buy before a `preview`/`production` build. |
| 2 | **EAS account + `eas login`** on your machine | The dev client is an EAS cloud build. |
| 3 | **Physical Android phone**, USB debugging on | The 15-min screen-locked test is Android-first. |
| 4 | **Apple Developer Program** active (for the iOS half) | iOS dev-client + background-location entitlement. Can lag Android by a few days. |

Mapbox/Claude keys are **not** needed for Phase 1 (no map, no AI here).

I can't run any of these — they run on your Windows machine under your accounts.
I hand you exact commands; you run them and paste failures back.

---

## 1. EAS dev client + Transistorsoft wiring

Expo Go cannot load a native module, so Transistorsoft requires a **custom dev
client**. This section is "scaffolding Step 6–7, done for real."

### What I'll add to `mobile/`

- **Deps:** `expo-dev-client`, `react-native-background-geolocation`,
  `react-native-background-fetch` pinned to `^4.4.0` (the newer line whose
  Gradle expects a tsbackgroundfetch AAR that *is* registered in settings.gradle
  — the older 4.2.x line requires `tsbackgroundfetch:1.0.4` which Expo's
  `FAIL_ON_PROJECT_REPOS` settings.gradle never registers, and bg-fetch's own
  Expo config plugin is a no-op stub), `expo-gradle-ext-vars` (sets Gradle ext
  vars for Play Services location + tslocationmanager versions),
  `@react-native-async-storage/async-storage` (offline queue durability —
  see §2).
- **Config plugin:** convert `mobile/app.json` → `mobile/app.config.js` (so we
  can branch env-driven values) and register the Transistorsoft config plugin
  with the license key, Android foreground-service config (§3), and iOS
  background-location config (§4):

  ```js
  // app.config.js (shape, not final)
  plugins: [
    "expo-dev-client",
    ["react-native-background-geolocation", {
      license: process.env.TRANSISTOR_LICENSE,   // EAS secret, never committed
    }],
    "react-native-background-fetch",
  ]
  ```

- **EAS secret:** `eas secret:create --name TRANSISTOR_LICENSE --value <key>` so
  the license never enters git. (You run this.)

### Build profiles (`mobile/eas.json`)

The existing `development` profile already has `developmentClient: true` +
`distribution: internal` — good. I'll confirm Android produces an installable
`.apk` (not `.aab`) for the dev client by adding
`"android": { "buildType": "apk" }` under `development`.

### Commands you'll run

```powershell
# from mobile/
npx expo install expo-dev-client react-native-background-geolocation `
  react-native-background-fetch @react-native-async-storage/async-storage
eas build --profile development --platform android
# install the resulting .apk on the phone, then:
npx expo start --dev-client
```

**Done when:** the dev client launches on your Android phone, loads the JS
bundle from `expo start`, and the Transistorsoft permission prompt appears (this
was the Phase 0 acceptance criterion that was never reached — we close it here).

---

## 2. RN recorder — reuse the existing wire format

The web recorder (`frontend/src/hooks/useTrackRecorder.js`) and its wire shape
are proven against the backend. We **reuse the contract, re-implement the
platform glue** — exactly the ADR's "reuse logic, not UI" principle.

### What carries over unchanged (the contract)

The flush body to `POST /api/races/{id}/telemetry` stays identical. For Phase 1
(GPS only):

```json
{ "gps": [ { "t": "...", "lat": .., "lon": .., "sog_kts": .., "cog_deg": .., "gps_acc_m": .. } ] }
```

Backend caps: **100 GPS / batch** (`MAX_GPS_SAMPLES_PER_BATCH`), drop-on-200,
retry-on-non-200. We honour both client-side, as the web recorder does.

### Extract the wire helper into the shared package

`gpsPointToWire()` currently lives inside `useTrackRecorder.js`. I'll move it to
`packages/shared/src/telemetry.js` and export it from the barrel, so web and
mobile serialize identically and the contract test (§6) has one target. The web
recorder switches to importing it — a small, supervised change to existing web
code that I'll call out separately before touching it.

### What gets re-implemented for RN

| Web concern | RN replacement |
|---|---|
| `navigator.geolocation.watchPosition` / Capacitor watcher | **Transistorsoft** `BackgroundGeolocation.onLocation(...)` + `.ready()` + `.start()` |
| Screen Wake Lock | **Deleted** — irrelevant; the whole point is to run with the screen off |
| `localStorage` queue | **AsyncStorage** (async API; same per-race key scheme `sailline.trackQueue.<raceId>`) |
| `apiFetch` (web) | RN `apiFetch` against the Cloud Run origin directly + Firebase ID token (auth wiring from scaffolding Step 5; full auth UI is Phase 2) |
| IMU sampler + calibration + axis-detect | **Not ported in Phase 1** (Phase 4) |

### Position normalization (note the shape difference)

Transistorsoft's location object is **not** the Capacitor shape. It's nested:
`location.coords.{latitude, longitude, speed (m/s), heading (deg), accuracy (m)}`
and `location.timestamp` (ISO string). So the RN normalizer differs from
`frontend/src/lib/geolocation.js::normalizePosition('native')`. The output
point shape stays `{ recorded_at, lat, lon, speed_kts, heading_deg, gps_acc_m }`
(× `MS_TO_KTS = 1.943844`), so everything downstream of the normalizer is shared.

### Deliberate decision: app-managed queue, NOT Transistorsoft autoSync

Transistorsoft ships its own SQLite persistence + HTTP auto-POST. We **do not**
use it. Reasons (flagging to avoid silent debt):

- Its HTTP layer posts the plugin's own location schema, not our batched
  `{gps:[...]}` telemetry shape.
- Firebase ID tokens expire hourly and need refresh per-request; threading that
  through the plugin's native HTTP is fragile.
- The web recorder's queue/flush/drop-on-ack logic is already proven against
  this exact endpoint.

So: Transistorsoft is the **capture engine only**; the JS recorder owns
queueing (AsyncStorage), batching (≤100), flushing (30 s timer + 100-point
trigger), and drop-on-ack. This keeps the proven contract and avoids a
schema/auth mapping layer inside the native plugin.

### New files

- `mobile/src/recorder/useTrackRecorder.ts` — RN port (GPS-only).
- `mobile/src/recorder/backgroundGeolocation.ts` — Transistorsoft adapter +
  normalizer (the RN analogue of `lib/geolocation.js`).
- `mobile/src/recorder/queue.ts` — AsyncStorage-backed per-race queue.
- `mobile/src/api.ts` — RN `apiFetch` (Cloud Run base URL + Firebase token).
- `packages/shared/src/telemetry.js` — extracted `gpsPointToWire` (+ barrel export).

---

## 3. Android foreground-service notification

Android kills background location without a **foreground service + persistent
notification**. Transistorsoft runs the service; we own the copy and the
battery-optimizer prompt.

Config (in the app.config.js plugin block / `.ready()` options):

```js
notification: {
  title: "SailLine — recording your race",
  text: "Capturing GPS while the screen is off.",
  channelName: "Race tracking",
  priority: BackgroundGeolocation.NOTIFICATION_PRIORITY_LOW, // quiet, no sound
  sticky: true,
},
foregroundService: true,
enableHeadless: false,   // we don't need JS after app termination in Phase 1
```

- **Battery optimization:** on aggressive OEMs (Xiaomi/OnePlus/some Samsung) the
  OS still throttles the service. Transistorsoft exposes
  `requestToggleTrackingPermission` / device-ignore-battery-optimizations
  helpers; I'll surface a one-time "allow unrestricted battery" prompt in the
  permission/onboarding flow. **Your physical-device test is what proves this**
  on your actual phone model.
- Permission flow: request **While-Using** first, then escalate to
  **Always/background** with a plain-language rationale screen (Android 11+
  requires the two-step escalation; a single "Always" request is silently
  downgraded).

---

## 4. iOS background-location config

iOS needs three things or background fixes stop at lock:

1. **`UIBackgroundModes` = `location`** — set via the config plugin
   (`ios.infoPlist.UIBackgroundModes: ["location"]`).
2. **Purpose strings** in Info.plist:
   - `NSLocationWhenInUseUsageDescription`
   - `NSLocationAlwaysAndWhenInUseUsageDescription`
   Draft copy: *"SailLine records your boat's track during a race, including
   while your screen is off, so your route and performance are captured
   continuously."*
3. **Always authorization** + don't let iOS pause us:
   ```js
   locationAuthorizationRequest: "Always",
   pausesLocationUpdatesAutomatically: false,
   stopOnTerminate: false,
   showsBackgroundLocationIndicator: true, // blue bar; App Review expects honesty
   ```

iOS testing is **physical iPhone only** (no Simulator without a Mac; EAS builds
the IPA in the cloud). The App Review background-location justification is a
Phase 6 paperwork item — noted, not done here.

---

## 5. The 15-minute screen-locked acceptance test

This is the criterion that **never passed** on web/Capacitor (browsers suspend
`watchPosition` on lock; the Capacitor shell was never finished). It's yours to
execute on a real phone, in motion.

### Procedure

1. Create a throwaway race on the phone (or reuse one; Phase 2 builds the create
   UI — until then I'll give you a minimal test harness screen / a seeded
   `race_id`).
2. Start recording. Confirm the Android notification (§3) appears.
3. **Lock the screen.** Put the phone in a pocket/bag.
4. Move continuously for **15 minutes** — drive, bike, or walk a varied path
   (turns matter; they exercise `cog_deg`).
5. Unlock, stop recording, let the final flush complete (watch `queueLength`
   hit 0).

### Pass criteria

Query `track_points` for the session and check **gap continuity**, not just row
count:

```sql
-- Expect ~900 points at ~1 Hz over 15 min, and NO gap > a few seconds.
SELECT
  count(*)                                              AS n_points,
  max(recorded_at) - min(recorded_at)                   AS span,
  max(gap)                                              AS worst_gap_seconds
FROM (
  SELECT recorded_at,
         EXTRACT(EPOCH FROM (recorded_at
           - lag(recorded_at) OVER (ORDER BY recorded_at))) AS gap
  FROM track_points
  WHERE session_id = '<race_id>'
) g;
```

- `span` ≈ 15 min.
- `worst_gap_seconds` small (target **< 10 s**); a multi-minute gap = the service
  was suspended = **fail**.
- Track is geographically continuous (no teleports across the locked window).

A clean pass on Android is the Phase 1 gate. iOS gets the same test once its
dev-client/entitlement path is proven. Follow with a real beer-can race capture
as the field validation.

---

## 6. Backend glue (small)

- **CORS:** *probably nothing to do.* Native RN `fetch` sends no `Origin`
  header, so the existing CORS middleware (`backend/app/main.py`) doesn't gate
  it. I'll verify against the deployed API from the dev client and only add an
  allow-list/`allow_origin_regex` entry if Expo *web* is ever used. **This
  contradicts the dev plan's "CORS for the mobile origin" line** — flagging per
  workflow rule 2.
- **Contract test:** the endpoint is already well-covered
  (`backend/tests/test_telemetry.py`, 18 tests — limits, 413/422, insert
  shapes). The remaining gap is a **wire-format contract fixture**: a small
  JSON sample emitted by the shared `gpsPointToWire` that's asserted to validate
  against the `TelemetryBatch`/`GpsSample` Pydantic models. I'll add that one
  test rather than re-doing endpoint coverage. (So "Known debt #2" is mostly
  already retired — verified, not assumed.)

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| OEM battery killing the service mid-race | Battery-optimization prompt (§3); your on-device test is the real proof |
| Transistorsoft license/dev-client friction | Trial works for dev; buy before `preview` build; iOS lags a few days |
| AsyncStorage write rate at ~1 Hz | Batch writes / debounce persistence; cap retained queue size |
| iOS "Always" downgraded to "While Using" | Two-step escalation + honest purpose strings; blue indicator on |
| Shared `gpsPointToWire` extraction touching web | Done as a separate, called-out change with the web build re-verified |

## 8. Done when

A screen-locked Android phone, in motion for 15 minutes, yields a continuous,
gap-free track in `track_points` (worst gap < ~10 s), end to end through the RN
recorder → AsyncStorage queue → `POST /api/races/{id}/telemetry`. iOS reaches
the same bar once its dev-client + Always-authorization path is validated on a
physical iPhone.

## 9. Build order (once approved)

1. Extract `gpsPointToWire` → `packages/shared`; re-point web; re-verify web build/tests (Windows).
2. Add deps + `app.config.js` + Transistorsoft plugin (§1).
3. You: `eas build --profile development --android`; install; confirm permission prompt.
4. RN recorder + adapter + AsyncStorage queue + RN `apiFetch` (§2).
5. Android notification + battery prompt + permission flow (§3).
6. iOS background config (§4).
7. Wire-format contract test (§6).
8. You: the 15-min screen-locked test (§5); then a real race capture.
