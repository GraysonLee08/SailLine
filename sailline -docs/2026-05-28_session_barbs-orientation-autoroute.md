# 2026-05-28 — Mobile: zoom-invariant barbs, orientation calibration, auto-route countdown

**Status: shipped + verified on-device tonight.** All three features
exercised on a real dev-client build during pre-race testing. Barbs
held screen size through pinch z11→z16, Zero-calibration zeroed the
heel readout as expected, auto-route toggle persisted per-race. No
on-water regressions surfaced during the race.

## What we worked on

Three mobile-app changes intended to be shipped before tonight's race:

1. **Zoom-invariant wind barbs** — barbs were drawn in fixed metres so
   they grew with zoom. Re-sized in screen pixels via
   `metersPerPixel(zoom, lat)` so they stay the same on-screen size
   regardless of zoom level.
2. **Phone orientation + Zero calibration** — mobile parity with the
   webapp's Fore-aft / Port-stbd / Zero pill row, including a live
   heel readout. Per-race calibration persisted in AsyncStorage.
3. **Auto-route on wind shift** — `BetterRouteBanner` now counts down
   10 s to auto-accept (the inverted Google Maps pattern: user must
   actively Decline to keep the current route). Per-race toggle in the
   sheet, default ON.

## Files changed

### New
- `mobile/src/sensors/orientation.ts` — DeviceMotion adapter. Converts
  expo-sensors's radian rotation triplet to the W3C
  `DeviceOrientationEvent` shape `{alpha, beta, gamma}` in degrees so
  the shared `remapEulerToBoat` / `applyCalibration` helpers work
  unchanged. Module-level reading slot + reference-counted listener so
  multiple UI consumers share one IMU subscription. Optional `require`
  of `expo-sensors` so the app boots even if the dep hasn't been
  installed in a particular dev client yet.
- `mobile/src/hooks/useHeelGauge.ts` — RN port of
  `frontend/src/hooks/useHeelGauge.js`. Tick rate 5 Hz, gated on
  `enabled`. Exports `captureCalibration()` for the Zero button.
- `mobile/src/hooks/useOrientationSettings.ts` — per-race AsyncStorage
  for `{phoneAxis, calibration}`. Key prefix `sailline:orientation:`.
- `mobile/src/hooks/useAutoRouteSetting.ts` — per-race boolean for the
  auto-accept toggle. Key prefix `sailline:autoRoute:`. Default `true`.
- `mobile/src/components/OrientationControls.tsx` — Fore-aft / Port-stbd
  / Zero pill row + status line + optional live heel readout. Uses
  theme tokens (`colors.text.muted`, `colors.accent.route`, etc.).

### Modified
- `mobile/src/lib/barbGeometry.ts` — replaced fixed `*_M` constants with
  pixel targets (`TARGET_SHAFT_PX = 14`, etc.) plus a `sizesFor(zoom,
  lat)` helper that converts to metres via Web Mercator
  `metersPerPixel`. `buildBarbFeatures` and `buildAllBarbFeatures` now
  take an optional `zoom` (default 11) so existing callers keep working.
- `mobile/src/components/WindBarbLayer.tsx` — accepts `zoom?: number`,
  passes through to `buildAllBarbFeatures`, adds `zoom` to the
  `useMemo` dep set so the geometry rebuilds on pinch/zoom.
- `mobile/app/(app)/index.tsx` — feeds `viewport?.zoom` into
  `<WindBarbLayer>`; mounts `useOrientationSettings` +
  `useAutoRouteSetting` per-race; passes both to `RaceDetailSheet` and
  threads `autoAcceptSeconds` to the banner.
- `mobile/src/components/RaceDetailSheet.tsx` — accepts new
  `orientation` and `autoRoute` props; renders the auto-route toggle
  inside the route block and a dedicated Orientation section.
- `mobile/src/components/BetterRouteBanner.tsx` — added countdown timer
  + progress bar + Decline button. When `autoAcceptSeconds > 0`, the
  banner auto-fires `onAccept` after the countdown; the user taps
  Decline to cancel. When `autoAcceptSeconds === 0`, falls back to the
  old manual Use/Dismiss layout.
- `mobile/package.json` — added `expo-sensors ~15.0.0` and
  `expo-screen-orientation ~9.0.0` (the latter isn't used yet but the
  install matches the SDK ahead of time). **Run `npx expo install
  expo-sensors expo-screen-orientation` to lock the SDK-matched
  versions before building.**

## Decisions made

- **Pixel target = 14 px shaft** (calibrated to match what the old
  fixed-800 m shaft looked like at z11, lat 42°). Tweakable in
  `barbGeometry.ts` if it reads too small.
- **Optional `expo-sensors` require** rather than a static import. Keeps
  the dev-client from crashing if someone runs the app before the new
  EAS build has been installed. Graceful fallback to "Heel/pitch
  unavailable on this device."
- **Per-race storage for calibration AND auto-route toggle.** Same
  rationale as the webapp: club regatta sharing one phone shouldn't
  smear calibration across boats. Keys are namespaced by raceId.
- **Auto-accept ON by default, 10 s countdown.** Matches user steer:
  "racing UX should default to the smarter route unless the skipper
  actively says no." 10 s is enough time to glance at the savings and
  decline if it's a weird suggestion.

## Open items / next steps

1. **Run on Windows before the race:**
   ```powershell
   cd E:\Personal\Coding\SailLine\mobile
   npx expo install expo-sensors expo-screen-orientation
   npx tsc --noEmit
   eas build --profile development --platform android
   ```
   The new orientation feature is a NATIVE module (expo-sensors uses
   the IMU), so a fresh dev-client build is required. Expo Go won't
   see it.
2. **No backend changes.** All three features are mobile-only.
3. **No IMU sample upload yet.** The mobile recorder still flushes GPS
   only. Wiring the heel/pitch stream into `useTrackRecorder` and
   `/api/races/:id/telemetry` (the IMU half of the wire schema) is
   still Phase 4 work — explicitly out of scope for tonight.

## Technical debt flagged

- **Bash mount truncation.** The sandbox bash still reads files
  truncated, so verification of `package.json` + a few other JSON files
  had to go through the Read tool. Memory entry already exists; calling
  it out again as a reminder.
- **`expo-screen-orientation` added but unused.** Pre-installed for the
  next session if we want to lock the app to portrait. Drop from
  package.json if we don't end up needing it.
- **Auto-accept timer doesn't pause** if the user pulls down the sheet
  to read the savings — it just keeps counting. Could add a "tap
  banner to pause" later, but that's polish.
- **No unit test for `metersPerPixel`** at the lat-extreme bounds. Math
  is standard Web Mercator; risk is low.

## Sources

- [BetterRouteBanner.tsx](file:E:/Personal/Coding/SailLine/mobile/src/components/BetterRouteBanner.tsx)
- [barbGeometry.ts](file:E:/Personal/Coding/SailLine/mobile/src/lib/barbGeometry.ts)
- [OrientationControls.tsx](file:E:/Personal/Coding/SailLine/mobile/src/components/OrientationControls.tsx)
- [useHeelGauge.ts](file:E:/Personal/Coding/SailLine/mobile/src/hooks/useHeelGauge.ts)
- [useOrientationSettings.ts](file:E:/Personal/Coding/SailLine/mobile/src/hooks/useOrientationSettings.ts)
- [useAutoRouteSetting.ts](file:E:/Personal/Coding/SailLine/mobile/src/hooks/useAutoRouteSetting.ts)
- [RaceDetailSheet.tsx](file:E:/Personal/Coding/SailLine/mobile/src/components/RaceDetailSheet.tsx)
- [index.tsx](file:E:/Personal/Coding/SailLine/mobile/app/(app)/index.tsx)
