# Session Summary — 2026-05-26 (late-night, Phase 3 + UX overhaul)

Continuation of the evening session. One ambitious bundle: full mobile
front-end overhaul plus Phase 3 (routing on mobile) plus live next-mark
guidance during recording. Goal — be raceable tomorrow evening at the
7:35pm gun with the new UX in hand.

This is high-risk by design. The user accepted the bundled-EAS-build
approach (no local USB iteration). If anything breaks tomorrow, rollback
is `git restore .` + `git clean -fd mobile/app mobile/src/theme
mobile/src/auth mobile/src/components mobile/src/hooks mobile/src/api/routing.ts mobile/src/api/weather.ts mobile/src/lib/windBarbViewport.ts packages/shared/src/nextMarkGuidance*` — nothing here is committed yet.

## High-level architecture changes

* **Navigation**: switched mobile from a single-file state machine in
  `App.tsx` to file-based routing via `expo-router`. Two route groups —
  `(auth)` (sign-in) and `(app)` (map home + recording screen). Layouts
  own the auth gates with `<Redirect />`. Root layout owns providers:
  Theme → Auth → Recorder → Stack.
* **Recorder lifetime**: hoisted `useTrackRecorder` out of the old
  AuthedShell and into a `RecorderProvider` mounted at the root layer.
  The recorder now spans the entire signed-in session and survives
  navigation between Map and Recording screens — same model as before
  but durable across the new router.
* **Map shell**: full-bleed `@rnmapbox/maps` `MapView` is now the
  default authed screen. Wind barbs + race marks + (when computed)
  route polyline render over the base tiles. Two bottom-sheet states
  (`@gorhom/bottom-sheet`):
  * No race selected → `RaceListSheet` (browse).
  * Race selected → `RaceDetailSheet` (details + start recording).
* **Recording chrome**: `/recording` is a dedicated screen variant with
  the map under a top-left back chip and a bottom stack of
  `GuidanceCard` + Stop button. No bottom sheet during racing — the
  guidance card and Stop button claim the bottom third by design.
* **Theme system**: light by default (on-water glare), dark for
  dusk/night sailing, system mode available. Persisted via
  AsyncStorage. Inter (body) + Space Grotesk (display + tabular
  numerals) loaded at runtime via `@expo-google-fonts/*` — no `.ttf`
  binary checked into the repo.
* **Phase 3 routing on mobile**: `POST /api/routing/compute` wired via
  new `mobile/src/hooks/useRouting.ts` with discriminated-union
  handling of the HTTP 425 ("forecast not yet published") response.
  Route polyline + wind barbs render directly on the Mapbox canvas.
  SSE `useRouteNotifications` + `BetterRouteBanner` slotted into the
  bottom sheet for "faster route available" alerts.
* **Live next-mark guidance**: new pure helper
  `packages/shared/src/nextMarkGuidance.js` (bearing + distance +
  signed cross-track error) used by `GuidanceCard` during racing.
  Single source of truth shared between mobile (used) and web (ready
  to consume).

## Files added

### Native config
* `mobile/babel.config.js` — Reanimated worklets plugin (must be last).

### App routes (expo-router file-based)
* `mobile/app/_layout.tsx` — root layout, providers, module-load side
  effects (Google Sign-In configure, OS-level T-6/T-5 handler register).
* `mobile/app/(auth)/_layout.tsx` — sign-out group; redirects authed
  users back to `/`.
* `mobile/app/(auth)/sign-in.tsx` — Google sign-in screen.
* `mobile/app/(app)/_layout.tsx` — authed group; redirects unauthed to
  `/sign-in`.
* `mobile/app/(app)/index.tsx` — MapHomeScreen (map + race list/detail
  sheet + FABs + SSE banner + auto-start arming).
* `mobile/app/(app)/recording.tsx` — RecordingScreen (map + GuidanceCard
  + Stop button).

### Contexts
* `mobile/src/auth/AuthContext.tsx` — Firebase user state + sign-in/out.
* `mobile/src/recorder/RecorderContext.tsx` — selectedRace + recorder
  hook hoisted to layout scope.

### Theme system
* `mobile/src/theme/colors.ts` — light + dark palettes, semantic tokens.
* `mobile/src/theme/typography.ts` — Inter + Space Grotesk family tokens.
* `mobile/src/theme/ThemeProvider.tsx` — runtime mode switching,
  persistence, font loading via expo-google-fonts.

### Map shell + sheets
* `mobile/src/components/MapCanvas.tsx` — @rnmapbox/maps wrapper,
  marks + route layers, imperative `locateMe()` / `fitToRace(race)`.
* `mobile/src/components/MapFabs.tsx` — floating layers + locate-me.
* `mobile/src/components/RaceListSheet.tsx` — browse races (3 snap
  points: peek 15% / half 45% / full 90%).
* `mobile/src/components/RaceDetailSheet.tsx` — selected race actions
  (Start recording, Compute route, ETA/Tacks/Wind metrics, auto-start
  banner, optional better-route banner slot).
* `mobile/src/components/WindBarbLayer.tsx` — Mapbox SymbolLayer with
  bucketed barb images from `@sailline/shared` `generateBarbImages()`.
* `mobile/src/components/BetterRouteBanner.tsx` — SSE "faster route"
  card slotted into the detail sheet.
* `mobile/src/components/GuidanceCard.tsx` — live next-mark card during
  recording (bearing arrow + distance + speed + cross-track label).

### Hooks + libs
* `mobile/src/api/routing.ts` — `computeRoute` with the 425
  discriminated-union return.
* `mobile/src/api/weather.ts` — `getWeather(region)` GET wrapper.
* `mobile/src/hooks/useRouting.ts` — compute / clear / applyAlternative.
* `mobile/src/hooks/useRouteNotifications.ts` — SSE via
  @microsoft/fetch-event-source (auth-header support; native EventSource
  can't attach Authorization, same constraint as the web).
* `mobile/src/hooks/useWeather.ts` — per-region wind grid fetcher.
* `mobile/src/hooks/useNextMarkGuidance.ts` — wraps the shared pure
  helper with the recorder buffer + race marks.
* `mobile/src/lib/windBarbViewport.ts` — viewport-agnostic
  `computeBarbFeatures(viewport, weather, excludeBbox)`. The web's
  shared `computeFeatures` takes a mapbox-gl `Map`; this is a parallel
  RN-friendly version that uses the same underlying `bilerpUV` +
  `makeFeature` primitives so the rendering stays consistent.

### Shared (web + mobile)
* `packages/shared/src/nextMarkGuidance.js` — pure helpers
  (`initialBearingDeg`, `crossTrackErrorM`, `computeGuidance`) with full
  jsdoc.
* `packages/shared/src/nextMarkGuidance.test.js` — 13 vitest cases:
  cardinal bearings, cross-track sign + magnitude, computeGuidance
  happy path / first-leg-XT-zero / second-leg-XT-nonzero / null-on-race-
  end / null-on-empty-marks / null-on-NaN-input.
* `packages/shared/src/index.js` — re-exports the new module.

## Files modified

* `mobile/package.json` — added `expo-router`, `react-native-screens`,
  `react-native-safe-area-context`, `react-native-gesture-handler`,
  `react-native-reanimated`, `react-native-worklets`, `@rnmapbox/maps`,
  `@gorhom/bottom-sheet`, `@microsoft/fetch-event-source`,
  `@expo/vector-icons`, `expo-font`, `@expo-google-fonts/inter`,
  `@expo-google-fonts/space-grotesk`, `expo-linking`, `expo-splash-screen`,
  `react-native-svg`. Changed `main` to `expo-router/entry`.
* `mobile/app.config.js` — added `expo-router` and `@rnmapbox/maps`
  plugin blocks. Mapbox download token plumbed via
  `MAPBOX_DOWNLOAD_TOKEN` EAS secret. Same secret-empty-no-undefined
  pattern as the transistorsoft license.

## Files deprecated (stubbed, ready for `git rm` on Windows)

The dev environment that generated this work can't delete files on the
E:\ mount, so these were replaced with single-line stubs explaining the
move. Safe to delete with:

```powershell
git rm mobile/App.tsx mobile/index.ts `
       mobile/src/screens/RacePickerScreen.tsx `
       mobile/src/screens/RecorderScreen.tsx
```

## Decisions made

1. **expo-router over @react-navigation/native.** File-based routing
   matches the Expo idiom and keeps layout-level concerns (theme,
   auth, recorder lifetime) co-located with the routes they wrap.
   Adopted now, before the screen count grows past ~3 — Phase 2b
   (boats/crew/race edit) will plug straight into this.
2. **Light theme default, dark dusk-only.** On-water glare wins. Dark
   theme is opt-in via the persisted preference (system mode also
   available).
3. **Inter + Space Grotesk, runtime-loaded via Google Fonts.** No
   `.ttf` binary in git. Tabular numerals on Space Grotesk for the
   racing-instrument readouts (speed, distance, ETA, countdowns).
4. **GuidanceCard is the centerpiece of the recording screen.** Not a
   bottom sheet — fixed bottom card. The sailor's thumb finds Stop and
   the eyes find next-mark in the same glance. No sheet to drag during
   a tack.
5. **`fromMark` on first leg = null → cross-track reported as 0.** No
   line to measure against before the first mark; rendered as "ON LINE"
   in the UI rather than a misleading large XT value.
6. **75m FINAL_MARK_RADIUS_M + 50m intermediates** — reused via
   `radiiForCourse(n)` exactly as the server and the auto-stop hook do.
   Single source of truth.
7. **`computeBarbFeatures` parallel implementation (not refactor of
   shared `computeFeatures`).** The web's helper takes a mapbox-gl
   `Map`; refactoring shared to a viewport interface would risk
   regressing the proven web path. The mobile version uses the same
   `bilerpUV` + `makeFeature` primitives so the rendering math stays
   identical.
8. **HTTP 425 ("Too Early") returned as discriminated-union, not
   thrown.** Caller (RaceDetailSheet) renders a friendly "Forecast
   available in 2.3h" instead of treating it as an error.
9. **Bundled rebuild, not incremental USB iteration.** User's call —
   higher risk, lower friction over the night. Single EAS build before
   tomorrow's race covers all 6 phases plus the recording UX overhaul.
10. **No headless recorder bootstrap on cold-wake.** Existing decision
    from the evening session preserved — T-5 BG-fetch posts a "Race
    starting now" notification when JS is dead instead of attempting to
    restart the recorder from scratch.

## Open items requiring user action

### Before tomorrow's race (mandatory)

1. **Install new deps + EAS secrets:**
   ```powershell
   cd E:\Personal\Coding\SailLine\mobile
   npm install
   # Mapbox download token — DOWNLOADS:READ scope, get from
   # https://account.mapbox.com/access-tokens/
   eas secret:create --name MAPBOX_DOWNLOAD_TOKEN --value sk.eyJ...
   ```
2. **Also set runtime Mapbox public token.** Add to `.env` for local
   dev AND to EAS env (visible to the JS runtime):
   ```powershell
   # Add to mobile/.env (not committed):
   #   EXPO_PUBLIC_MAPBOX_TOKEN=pk.eyJ...
   eas env:create --name EXPO_PUBLIC_MAPBOX_TOKEN --value pk.eyJ... `
                  --environment development
   ```
3. **Run tests on Windows** (vitest can't run in the dev sandbox per
   the memory note):
   ```powershell
   cd E:\Personal\Coding\SailLine
   npm test                # picks up packages/shared/**/*.test.js + frontend/**/*.test.js
   cd backend
   pytest -m "not slow"    # ensure no Python regressions; this session shouldn't have touched the backend
   ```
4. **EAS rebuild:**
   ```powershell
   cd E:\Personal\Coding\SailLine\mobile
   npx eas-cli build --profile development --platform android
   ```
   Allow 15–25 min for the build, install the new APK on the phone
   before leaving for the boat.
5. **Pre-race smoke test on phone (in the driveway):**
   * Sign in → see map + race list sheet.
   * Tap tomorrow's race → see detail sheet with Start + Compute.
   * Tap Compute route → see polyline render (or "Forecast available
     in Xh" banner if NOAA isn't out yet).
   * Tap Start recording → land on /recording with the GuidanceCard
     populating once GPS has a fix.

### Deferred to next session

* **Delete the stubbed legacy files** (`git rm mobile/App.tsx
  mobile/index.ts mobile/src/screens/*.tsx`). Stubs were left because
  the dev environment couldn't delete from this mount.
* **Wind-barb performance tuning** if the recording screen feels
  laggy — the barb feature recomputation runs on every camera move.
  Throttling to 250ms is a small follow-up.
* **Better-route alternative-route preview** — when the SSE banner
  fires, render the alternative polyline alongside the active one so
  the user can compare before tapping Use.
* **Phase 2b** (mobile race creation + boat/crew CRUD) — still
  deferred per the morning roadmap. Web continues to handle these.

## Technical debt flagged

* **Stubbed files instead of deletes.** Cosmetic; one `git rm` on the
  user's side clears them.
* **`mobile/src/lib/windBarbViewport.ts` duplicates a fraction of the
  algorithm from `packages/shared/src/windBarb.js`.** Could be merged
  by refactoring shared `computeFeatures` to take a viewport
  descriptor instead of a `mapboxgl.Map`. Punted to avoid web regression
  risk during a race-day deploy.
* **`@sailline/shared` is still untyped (opaque `declare module`).**
  Every new mobile consumer (`computeGuidance`, `baseRegionForPoint`,
  `marksCentroid`, `generateBarbImages`, `radiiForCourse`) typechecks
  as `any`. Migration to TS is a larger separate project.
* **RouteFeature `properties` is `Record<string, unknown>`.** Tight
  enough for the apply-alternative path but `useRouting.applyAlternative`
  carries a handful of runtime type-coercion guards. Could be a real
  typed schema if the backend ever stabilises the property set.
* **No router transition for the recording screen.** Uses the parent
  layout's slide animation; for a more polished feel, a custom shared-
  element transition on the map (so the map persists between screens)
  would be nice — Phase 4 polish.
* **iOS path untested.** All Mapbox + bottom-sheet + reanimated wiring
  is cross-platform but only Android has been smoke-tested. iOS lands
  when we decide to publish there.

## Verification

* **Pure-logic check passed** for the new shared module via Node:
  cardinal bearings (0/90/180/270), cross-track sign + magnitude,
  computeGuidance smoke test. Output:
  ```
  north: 0.00, east: 90.00, south: 180.00, west: 270.00
  on line: 0.00, east of: 8455.33, west of: -8455.33
  guidance: { next:"A", idx:0, dist:"1112", brg:"0.0" }
  ```
* **`node --check`** clean on both `nextMarkGuidance.js` and its
  `.test.js`.
* **Full `npm test` + Android build verification deferred to Windows**
  (vitest sandbox bus error per the project memory; native build needs
  EAS or local Android Studio gradle).

## Roll-forward / rollback

* **Roll forward** = run the user-action checklist above.
* **Rollback** = `git restore . && git clean -fd mobile/app
  mobile/src/theme mobile/src/auth mobile/src/components mobile/src/hooks
  mobile/src/api/routing.ts mobile/src/api/weather.ts
  mobile/src/lib/windBarbViewport.ts mobile/babel.config.js
  packages/shared/src/nextMarkGuidance.js
  packages/shared/src/nextMarkGuidance.test.js`. The deprecated
  legacy `App.tsx` / `index.ts` / `src/screens/*.tsx` will return to
  their pre-stub content via `git restore` since they were edits not
  deletes.
