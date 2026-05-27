# Session Summary — 2026-05-26 (overnight, post-Phase-3 fixes)

Continuation of the late-night Phase-3 push. The initial EAS build from
that session loaded but exposed a long tail of integration bugs in the
real-device environment. This session was diagnostic-driven: trace each
failure to root cause, ship the fix, retest. By end of session the app
is in a state where every race-day-critical flow works (sign-in, race
list, route compute, recording, live mark guidance, wind barbs, SSE
better-route stream); a handful of nice-to-have polish items remain
queued for after the race.

## Bugs fixed (in order encountered)

### 1. EAS build pipeline
- `@expo-google-fonts/space-grotesk@^0.4.2` didn't exist (latest is 0.4.1) — pinned down.
- Mapbox secrets workflow: `sk.*` download token via `eas secret:create`, `pk.*` runtime token via `eas env:create --visibility sensitive`. The user's first attempt picked "secret" visibility for the runtime token; EAS rejects this for `EXPO_PUBLIC_*` vars because they bake into the bundle.
- `Out-File -Append .env` writes UTF-16 BOM by default; Expo's env loader can't parse it (rendered as `��EXPO_PUBLIC_MAPBOX_TOKEN`). Replaced with `[System.IO.File]::WriteAllText` + explicit UTF-8 encoding.

### 2. Bundler config (monorepo)
- Missing `babel-preset-expo` after switching to expo-router entry — workspace hoisting put babel at root where the preset wasn't visible. Installed directly in `mobile/`.
- Two copies of React in the tree (`react@19.1.0` at `mobile/node_modules`, `react@18.3.1` at workspace root from the Vite frontend). Originally tried `disableHierarchicalLookup: true` — cascaded into "transitive X not found" errors for `@expo/metro-runtime`, `expo-asset`, `@react-native/virtualized-lists`. Landed on a surgical `resolver.resolveRequest` override that forces ONLY `react` to mobile's copy, leaving everything else resolving hierarchically as before.

### 3. SSE notifications
- `@microsoft/fetch-event-source` is browser-only — calls `document.addEventListener` for tab-visibility handling, crashing React Native at mount with `ReferenceError: Property 'document' doesn't exist`.
- Swapped to `react-native-sse` (the standard RN-compatible drop-in with custom-header support).
- Added `lineEndingCharacter: "\n"` because FastAPI + sse-starlette emit LF without CR, and react-native-sse can't auto-detect on the initial frame.

### 4. Wind barbs (the long one)
This was a multi-stage diagnosis:
- Symptom: barbs never rendered.
- First attempt was `Images` + SymbolLayer with SVG data-URL icons. Android's native bitmap loader doesn't decode `data:image/svg+xml;...` — silent failure.
- Added diagnostic `console.log`s to `useWeather`, `useRouting`, and the barb feature builder. Confirmed: grid loads (260×601 HRRR), viewport callbacks fire, features compute correctly (8–15 per viewport at zoom 11+, valid bucket + dir), but nothing visible on screen.
- Tried text symbols with `textFont: ["Open Sans Regular", ...]`. Still nothing — most likely the requested font isn't in the Light/Outdoors style's glyph store and Mapbox silently no-ops the layer.
- Stripped to a minimum (constant glyph, no font directive, hardcoded color). Triangles appeared but with no rotation — `textRotate` was being ignored.
- Final solution: pure LineLayer-based barbs. Pre-compute the actual shaft + flag + pennant geometry in JS as a GeoJSON FeatureCollection, render via a single LineLayer. Real meteorological barbs, sailor-recognisable, GPU-accelerated, no glyph dependency.
- Tuned styling for visibility: 3px stroke + 5px white casing underneath, near-black for 5-9 kt (was invisible navy-on-water), 4px calm-station dots with 2px white halo.
- **Switched basemap to `Mapbox.StyleURL.Light`** (was Outdoors) so the desaturated whites/greys/blacks let the wind + route + marks pop. User-requested.

### 5. FABs
- Cluster sat under the system status bar/notch. Added `useSafeAreaInsets()` and moved the top offset to `insets.top + 16`.
- Locate-me FAB silently no-op'd. Was using `setCamera({followUserLocation: true, ...})` — that variant of `setCamera` is silently ignored in `@rnmapbox/maps` v10 when the `<Camera>` was mounted without following. Replaced with: read `Mapbox.locationManager.getLastKnownLocation()` → call `setCamera({centerCoordinate, zoomLevel, animationDuration})` with explicit coords. The previous "moves part way but doesn't finish" symptom was the declarative `followUserLocation` flip aborting the imperative animation; dropped the flip.
- Compass FAB built-in (Mapbox's display-only compass) doesn't expose a tap handler. Replaced with a custom compass FAB that cycles north → follow-device-heading → north on each tap. Icon rotates with the map's heading so the user sees the relationship.

### 6. Saturday race compute "silently fails"
- Wasn't actually broken — the API returned `kind=ok` with a real route. The user couldn't see the result because the sheet was at the 32% peek snap and the route metrics (ETA / Tacks / Wind) were below the visible area.
- Fix: auto-expand the sheet to its 75% snap whenever `routePending` or `routeError` arrives. Successful computes still leave the sheet at peek (they don't need attention).

### 7. Test fixtures (backend regression from prior session)
- `tests/test_telemetry.py` `_race_row()` helper didn't include the new `started_at` + `start_at` fields the production code now reads. Updated.
- `tests/test_track_ingest.py` had three inline `fetchrow.return_value` dicts with the same issue. Updated each.
- `_rounding_points` + `_rounding_gps_batch` were tuned for the 50 m default radius but `radii_for_course(1)` now gives a single-mark course `FINAL_MARK_RADIUS_M=75 m`. Widened both helpers to span ±150 m so the points cleanly exit either radius.
- All 563 backend tests green after the fix.

## Files changed

### New files
- `mobile/src/lib/barbGeometry.ts` — pre-compute meteorological wind barb LineString features.

### Frontend / mobile rewrites
- `mobile/src/components/WindBarbLayer.tsx` — full rewrite to LineLayer-based real barbs.
- `mobile/src/hooks/useRouteNotifications.ts` — swapped @microsoft/fetch-event-source for react-native-sse.
- `mobile/src/components/MapCanvas.tsx` — locate-me, compass toggle, monochrome Light style, viewport now emits heading.
- `mobile/src/components/MapFabs.tsx` — three FABs (compass, layers, locate-me), safe-area-aware positioning.
- `mobile/src/components/RaceDetailSheet.tsx` — auto-expand on pending/error.
- `mobile/app/(app)/index.tsx` — viewport shape with heading, MapFabs prop changes.
- `mobile/app/(app)/recording.tsx` — viewport shape change.
- `mobile/src/hooks/useWeather.ts` — diagnostic console.log (kept).
- `mobile/src/hooks/useRouting.ts` — diagnostic console.log (kept).
- `mobile/metro.config.js` — react-only resolveRequest override.
- `mobile/babel.config.js` — newly created for reanimated worklets plugin.
- `mobile/app.config.js` — added expo-router + @rnmapbox/maps plugins, Mapbox download token plumbing.
- `mobile/package.json` — dep list reshuffled (added expo-router, mapbox, bottom-sheet, react-native-sse, expo-google-fonts pair, etc.).

### Backend test fixtures
- `backend/tests/test_telemetry.py` — `_race_row()` widened to include `started_at` + `start_at`; `_rounding_gps_batch` span doubled.
- `backend/tests/test_track_ingest.py` — three inline race-row dicts widened; `_rounding_points` span doubled.

### Frontend
- `frontend/vite.config.js` — vitest include pattern now picks up `packages/shared/src/**/*.test.js` (previously dropped 4+ shared tests including the new `nextMarkGuidance.test.js`).

## Decisions made

1. **Real meteorological barbs (LineLayer) over text-symbol arrows.** User explicitly wanted "what sailors are used to reading." Pre-computing geometry in JS is a few hundred more lines than text rotation but reliable across SDK versions and platforms.
2. **Monochrome basemap (Mapbox Light).** Desaturated base = overlays pop. Truly single-hue would require a custom Mapbox Studio style (~10 min, deferred to next session).
3. **Surgical resolveRequest override (only `react`) instead of disableHierarchicalLookup.** The latter cascades into transitive-not-found errors. The former is the smallest possible workaround for the duplicate-React problem.
4. **Defer to setTimeout(0) for imperative setCamera after state flip.** React state changes are batched; the declarative `<Camera>` prop hasn't propagated when the callback runs. setTimeout(0) gives the prop flush a chance before the imperative call.
5. **Auto-expand the detail sheet only on pending/error, not successful compute.** Successful compute leaves the sheet at peek because there's nothing the user needs to react to. Errors and "forecast not out yet" both demand attention.
6. **Compass heading-follow accepted as 90°-off for tomorrow's race.** Root cause is @rnmapbox/maps v10's heading-follow mode using GPS course rather than the device magnetometer. Fixing properly requires direct expo-sensors magnetometer wiring (~1h). Not worth landing 6h before a race without on-water testing.

## Open items / deferred to next session

### High value
- **Custom monochrome Mapbox style** (Mapbox Studio, ~10 min) — true whites/greys/blacks, no green parks, no blue water.
- **Compass heading-follow magnetometer fix** — wire `expo-sensors` Magnetometer directly to `setCamera({heading})` at ~10Hz.
- **Per-race forecast snapshot persistence** — new `race_forecast_snapshot` table on the backend, write at race-start, read fallback for past races whose live cycles have rotated out of Redis. Substantial backend work.
- **Continuous wind viz like Windy.com** — animated particle layer or u/v-interpolated heatmap. @rnmapbox/maps doesn't have a velocity layer built in; either custom WebGL or a third-party port. Multi-day.
- **Wind viz timeline** — scrub through the HRRR forecast hours; backend already has the data, needs a new endpoint that takes a `valid_time` param + a frontend scrubber.
- **Wave overlay** — entirely new feature. Backend needs WaveWatch III ingest (new Cloud Run Job, GCS archive, Redis cache, `/api/waves` endpoint), then a new frontend layer.

### Medium value
- **Improve "1-mark course" UX.** The "Test5.26.26.1317" race has 0 marks and the compute call returns "race must have at least 2 marks (start + finish)" — surfaces as a raw 400 error to the user. Should be a friendlier "this race needs marks" inline message.
- **Wind regardless of selected race** — currently shows at zoom 2 (whole CONUS = 2 barbs only). Better default would be to center on the user's location at first paint so they see a useful number of barbs.
- **Stubbed legacy files cleanup** — `mobile/App.tsx`, `mobile/index.ts`, `mobile/src/screens/RacePickerScreen.tsx`, `mobile/src/screens/RecorderScreen.tsx` are all stubs since the sandbox couldn't delete them. User can `git rm` on Windows.

### Low value
- Strip the diagnostic `console.log`s once the dust settles (`useWeather`, `useRouting`, `index.tsx` barbFeatures memo). Useful for on-water debug; can stay.

## Technical debt flagged

1. **Two copies of React in the monorepo tree.** Surgical resolveRequest works but the deeper fix is to align React versions across web + mobile, or to use npm `overrides` to force a single version.
2. **`mobile/src/lib/windBarbViewport.ts` duplicates fraction of `packages/shared/src/windBarb.js`.** Could be merged by refactoring shared `computeFeatures` to take a viewport descriptor.
3. **`@sailline/shared` still untyped** (opaque `declare module`). Mobile consumers (`computeGuidance`, `baseRegionForPoint`, `marksCentroid`, `radiiForCourse`) typecheck as `any`.
4. **iOS path untested.** All Mapbox + bottom-sheet + reanimated wiring is cross-platform but only Android has been smoke-tested.

## Verification

- Backend: `pytest -m "not slow"` → 563 passed, 3 skipped, 4 deselected.
- Frontend: `npm test` from `frontend/` → 170 passed across 15 test files (now including the previously-skipped `packages/shared/**/*.test.js`).
- On-device: every Phase-3 feature smoke-tested over USB; user signed off on each fix before moving to the next.

## Status going into tomorrow's race (2026-05-27, gun 19:35)

Race-day critical path: ✅
- Sign-in (auto from cached session)
- Race list browseable, map renders
- Tap race → details sheet with Start + Compute
- Compute route → route polyline draws on map, metrics populate
- Start recording → recording screen with map + live next-mark guidance + Stop
- Better-route SSE banner ready to fire when worker publishes

Nice-to-have but not blocking: 🟡
- Wind barbs visible (just tuned for visibility — verify on first morning launch)
- Compass north-snap works; heading-follow is 90° off (accept for tonight)
- Layers FAB toggle works
- Locate-me works (within last-known-fix accuracy)

Will fix tomorrow if breaks: 🔴
- Nothing identified.
