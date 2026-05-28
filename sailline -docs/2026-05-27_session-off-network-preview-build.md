# Session Summary — 2026-05-27 (off-network preview build)

Long session getting an off-home-network APK on the phone in time for
tonight's race. The end state is a working preview APK with the v5 of
`react-native-background-geolocation`, signed-in and rendering the map
over cellular. Race-ready.

The path there was much rockier than expected — five distinct blockers
stacked end-to-end. Lessons learned section at the bottom; worth a read
before the next mobile build.

## What we worked on

Goal: have a SailLine APK on the phone that works off home wifi for
tonight's race (gun 19:35).

Started from a dev-client-only flow: the existing dev-client APK loaded
the JS bundle from Metro on the home LAN, which doesn't work off-network.
Cut a standalone preview build (JS bundled in, no Metro needed). Hit
five issues in sequence; resolving the chain ate the afternoon.

## Files changed

### `mobile/eas.json`
- Made `preview` profile explicit: `developmentClient: false`,
  `android.buildType: "apk"`, added `channel: "preview"`. Plus added
  `production.channel: "production"` so EAS Update channels are set up
  for future JS-only patches.
- After the first build died with "lost connection to worker" (OOM),
  added `android.resourceClass: "large"` to the preview Android block.
  Larger workers consume ~2× plan minutes but the build doesn't fit on
  medium with Mapbox + Transistorsoft + reanimated + worklets + screens
  all compiling native code together.

### `mobile/package.json`
- Bumped `react-native-background-geolocation` from `^4.18.0` to
  `^5.1.1` once we discovered the license/wrapper version mismatch
  (see Decisions §1).

### `mobile/app.config.js`
- v4→v5 upgrade attempt bumped `tslocationmanagerVersion` from `"4.0.+"`
  to `"5.0.+"` on the (wrong) assumption that the native SDK major
  tracks the JS wrapper major. Maven gradle resolution failed — latest
  `com.transistorsoft:tslocationmanager` on Maven is `4.1.6`. Reverted
  to `"4.0.+"` with an inline comment explaining the gotcha so we don't
  do it again.

### `mobile/src/recorder/backgroundGeolocation.ts`
- Full rewrite for the v5 Config API. Every flat option from v4 is now
  nested under `geolocation` / `app` / `logger` / etc. Concretely:
  - `desiredAccuracy`, `distanceFilter`, `locationUpdateInterval`,
    `fastestLocationUpdateInterval`, `disableElasticity`,
    `locationAuthorizationRequest`,
    `pausesLocationUpdatesAutomatically`,
    `showsBackgroundLocationIndicator` → `geolocation: {...}`
  - `stopOnTerminate`, `startOnBoot`, `backgroundPermissionRationale`,
    `notification` → `app: {...}`
  - `logLevel` → `logger: { logLevel: LogLevel.Warning }`
  - `reset: true` stays top-level
  - Dropped `autoStart: false` (removed in v5; we call `.start()`
    explicitly anyway, so behavior unchanged)
  - Dropped `foregroundService: true` (implicit in v5)
- Constant renames: `DESIRED_ACCURACY_NAVIGATION` → `DesiredAccuracy.High`
  (Navigation is iOS-only in v5; High is the cross-platform max-accuracy
  equivalent), `LOG_LEVEL_WARNING` → `LogLevel.Warning`,
  `NOTIFICATION_PRIORITY_LOW` → `NotificationPriority.Low`.
- `Location.timestamp` is now typed `string | number` in v5; coerce via
  `new Date(location.timestamp).toISOString()` to land on a single
  canonical ISO 8601 string regardless of which the SDK gave us.
- `coords.speed` / `heading` / `accuracy` are now typed
  `number | undefined`. Added explicit `!= null` checks so TS narrows
  `undefined` out — `Number.isFinite()` doesn't narrow as a type guard
  even though it works at runtime.

### `mobile/src/theme/typography.ts`
- Removed both `as const`s on `TABULAR_FONT_VARIANT` and typed it as
  `TextStyle`. The old `readonly ["tabular-nums"]` tuple wasn't
  assignable to RN's mutable `FontVariant[]`.

### `mobile/src/components/RaceDetailSheet.tsx`, `mobile/src/components/RaceListSheet.tsx`
- Dropped `as const` on `SNAP_POINTS`. `@gorhom/bottom-sheet`'s
  `snapPoints` prop expects mutable `Array<string | number>`, not a
  readonly tuple.

### `mobile/src/components/MapCanvas.tsx`
- Loosened `handleCameraChanged`'s parameter type — `center` and
  `bounds.{ne,sw}` are `number[]` (Mapbox v11 Position type), not
  strict 2-tuples. Runtime code uses `[0]`/`[1]` indexing so the change
  is type-only.

### `mobile/src/components/WindBarbLayer.tsx`
- Cast `colorRamp` to `any` at the `lineColor` use site. The variable
  is a valid Mapbox `step` expression at runtime, but Mapbox v11's
  precise Expression-tuple union is painful to satisfy from a plain
  array. The cast is honest about the boundary.

### EAS environment variables (web dashboard, not in repo)
- Added `TRANSISTOR_LICENSE` (sensitive) for preview / development /
  production environments. **First attempt mis-pasted the entire
  PowerShell `eas env:create` command as the value** — the resulting
  build's license toast literally said `"Invalid license key: eas
  env:create --scope project --na…"`. Fixed via the web dashboard.
- Added `EXPO_PUBLIC_MAPBOX_TOKEN` (plain text) for preview /
  development / production. This was the LAST blocker — see Decisions
  §5.

## Decisions made

### 1. Buy a Transistorsoft license rather than fight v4-only paths

When the v4 dev client moved to a v4 release build, Transistorsoft
enforced license validation (debug builds are free; release builds need
a real license). Christopher (Transistorsoft) confirmed via email that
the auto-generated key from order #16384 is a **v5 key** and the wrapper
in our repo was on v4 — the version mismatch was the validation failure.
Two options offered: legacy v4 key, or upgrade to v5. Chose v5 to stay
current and avoid carrying an unsupported library version into Phase 2.

### 2. EAS `resourceClass: "large"` for Android preview

First build died with "lost connection to worker" mid-CMake during
reanimated/worklets native compile. Default `medium` workers have ~7GB
RAM which isn't enough with all our native modules building in parallel.
`large` workers cost ~2× per minute but it's the right trade for a
build that actually finishes. Kept in the preview profile permanently.

### 3. v5 upgrade now, in spite of race-day risk

User stated preference to upgrade rather than take the legacy v4 key.
Reasoning: legacy key would leave us stuck on an unsupported library
that we'd have to upgrade eventually anyway, and the upgrade scope was
contained (one usage site, configuration restructure, no business-logic
changes). Race-day risk acknowledged in writing; mitigations included
running `tsc --noEmit` before the EAS build so we knew the bundle would
at least compile.

### 4. Fix the 8 pre-existing tsc errors in the same session

User pushed back on my initial "leave them for later" framing.
Pushback was correct: they were all zero-runtime TS-strictness fixes,
took ~10 min, and now a clean `tsc --noEmit` is meaningful signal for
future sessions. Worth the time even on race day.

### 5. `EXPO_PUBLIC_*` vars are baked at build time, not runtime

The final blocker after license + v5 + memory + tslocationmanager
version was solved: APK launched, signed in via Google, then **crashed
immediately** in the Mapbox `MapView` constructor with
`MapboxConfigurationException: ... requires providing a valid access
token`. Logcat (via `adb`) showed the FATAL EXCEPTION clearly.

Root cause: `EXPO_PUBLIC_MAPBOX_TOKEN` exists in local
`mobile/.env` but was never set in EAS. Dev client builds had been
picking it up via Metro on the local machine. EAS builds in the cloud
have no view of the developer's `.env` — env vars must be set in the
EAS dashboard. Added the token there for all three environments
(preview, dev, production) so the production path is already correct.

### 6. EAS env vars via web dashboard, not CLI, when copy-pasting secrets

The "Invalid license key: eas env:create --scope project --na…" was a
copy-paste mishap into an interactive prompt. The web UI sidesteps every
shell quoting issue and is the right move for any sensitive value. The
CLI path with `--value '<key>' --force` works too, but the dashboard
is foolproof.

## Verification

On-device smoke test (Pixel 9 Pro XL, Android "CinnamonBun" preview,
release-mode preview APK, **cellular only — wifi off**):

1. App launches past splash, no license validation toast. Confirmed.
2. Google sign-in flow completes, lands in the authenticated app shell
   without the Mapbox crash. Confirmed.
3. App works off home network — race-day blocker resolved. Confirmed.

What was NOT tested in this session:
- Compute → polyline + metrics
- Start recording on a real race
- Foreground service notification appears
- Screen-locked recording continuity (Phase 1 acceptance criterion)

These will be exercised in tonight's race itself. None of them changed
in this session's code other than the v5 config shape rewrite — risk
is bounded.

## Open items / next steps

### High priority post-race
- **Confirm screen-locked recording works on v5.** The Phase 1
  acceptance test wasn't re-run after the v5 upgrade. If gaps appear
  in tonight's track, the v5 `geolocation` config (especially
  `disableElasticity` and `pausesLocationUpdatesAutomatically`) may
  need re-tuning against v5's defaults.
- **Verify `DeviceSettings.*` helper still works on v5.** The
  battery-optimization-exemption helper (`requestBatteryOptimizationExemption()`)
  is wrapped in try/catch so if the v5 API surface differs it'll
  silently no-op. Worth testing the prompt actually fires.

### Medium priority
- **Mobile session summary docs:** every successful build on EAS now
  burns ~15 min and ~2× plan minutes (large worker). Worth optimizing:
  - Pin specific versions in `app.config.js` so cache hits more often.
  - Consider whether `large` is really needed once gradle has cached
    the heavy bits.
- **Sync `mobile/.env` ↔ EAS env vars in docs.** The local-only
  `.env` and EAS web dashboard variables drifted today and we lost
  hours to the gap. Add a short README in `mobile/` listing every
  variable that must exist in BOTH places, with a checklist.

### Low priority
- The 8 pre-existing `tsc --noEmit` errors are all cleared. `tsc`
  baseline is now clean; CI could enforce that in the future.

## Technical debt flagged

1. **`expo-updates` was installed mid-build** during the first preview
   attempt (because the `preview.channel` config required it). We never
   exercised the OTA-update path — the channel is configured but no
   updates have been published. If we lean on OTA in Phase 2 it'll need
   actual testing.
2. **`tslocationmanagerVersion: "4.0.+"`** — even with the v5 JS
   wrapper. Christopher's docs confirm this is correct today, but if
   he ships a tslocationmanager v5.x native SDK we'll want to bump in
   sync.
3. **Mapbox `lineColor: colorRamp as any`** in `WindBarbLayer.tsx`.
   The cast hides a real expression-type mismatch that Mapbox's v11
   types are stricter about. A future cleanup could type `colorRamp`
   as a proper `ExpressionField` tuple.
4. **Pre-existing tsc strictness in 5 files cleaned up this session
   was all `as const` vs mutable-array issues.** The pattern keeps
   appearing because RN style props use mutable arrays everywhere.
   Worth a project-wide convention: avoid `as const` on tuples that
   go into RN style props; use typed exports (`: TextStyle`) instead.
5. **Two copies of React in the monorepo tree** (carried over from
   prior sessions — workaround is in `mobile/metro.config.js`).
6. **`mobile/src/lib/windBarbViewport.ts` duplicates fraction of
   `packages/shared/src/windBarb.js`** (carried over).
7. **`@sailline/shared` still untyped** (opaque `declare module` —
   carried over).
8. **iOS path untested** (carried over).

## Lessons learned

Captured separately because today's debug path was a sequence of
diagnostic mistakes that compounded. Worth internalizing.

1. **Diagnose the actual error before forming a theory.** I spent real
   time speculating about Transistorsoft v5 release-mode bugs and
   considered rolling back to v4 before we even captured logcat. When
   we finally did, the crash was Mapbox missing a token — entirely
   unrelated to the upgrade. Always: get the FATAL EXCEPTION first,
   theorize second.

2. **`EXPO_PUBLIC_*` env vars are inlined at bundle time, not read at
   runtime.** Local `.env` ≠ EAS env. This is documented but easy to
   forget when the dev client has been working off `.env` for weeks.
   Every new EAS environment needs every `EXPO_PUBLIC_*` var added
   independently.

3. **The Transistorsoft license is tied to the SDK major version.** Buy
   the license AFTER you've decided which major you're on, not before.
   Or expect to ask for a legacy key.

4. **The native SDK version (`tslocationmanager`) is not the same as
   the JS wrapper version (`react-native-background-geolocation`).**
   Don't assume wrapper-major → native-major sync. Always check the
   Setup docs for the canonical `ext` var values.

5. **Default EAS Android workers run out of memory** on a project with
   many native modules. `resourceClass: "large"` is the practical
   default for any release build with Mapbox + Transistorsoft +
   reanimated together. Mediocre default; loud failure mode ("lost
   connection to worker"); easy fix.

6. **Use the EAS web dashboard for sensitive env vars.** Interactive
   CLI prompts are where shell-paste mishaps live. The dashboard has
   a plain text field, no quoting concerns, copy-paste safe.

7. **Race tonight is racing, not shipping.** We got lucky this session
   — five blockers all surfaced before the gun. Next session involving
   release-mode native changes deserves a dedicated dry-run day, not a
   race-day morning slot.
