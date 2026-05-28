# 2026-05-28 — Recorder crash fix + recording-screen UX

Post-race feedback session. User finished a beer can race the previous evening
and surfaced three observations: (1) the start-of-race notification needs an
"Activate Recording Now" action button, (2) the actual track didn't render on
the map and they want a live actual-vs-planned overlay, (3) the race didn't
auto-end at the finish line. Investigation pivoted as we uncovered the actual
root cause was upstream of all three.

## What we worked on

### Investigation of `Beer Can Race 2` (race id `d996d576-dda7-4eb5-a022-9426f541ceb1`)

The `race_sessions` row had `started_at = 2026-05-28T00:35:00Z`,
`ended_at = ""`, `mark_passes = "[]"`. Diagnostic SQL against `track_points`
filtered to that race id returned one row, timestamped
`2026-05-27T13:52:58Z` — eleven hours before `started_at` and about three
minutes after the race was created. Zero telemetry shipped during the race
window. `started_at` had been backfilled by that single stray pre-race POST
via `track_ingest.detect_and_persist_new_passes`, which copies `start_at` into
`started_at` on first telemetry regardless of how stale the point is.

Confirmed via grep that no client code PATCHes `started_at` directly — the
backend's `track_ingest` is the only writer. The "raceish" `started_at` value
in the row is therefore a red herring and not evidence the recorder ran.

### Root cause: recorder crash on v5 named imports

`mobile/src/recorder/backgroundGeolocation.ts` was importing five members as
named exports from `react-native-background-geolocation`:

```ts
import BackgroundGeolocation, {
  Location, Subscription, DesiredAccuracy, LogLevel, NotificationPriority,
} from "react-native-background-geolocation";
```

Inspection of the installed v5.1.1 package showed `src/index.js` only has
`export default class BackgroundGeolocation`. The TypeScript types are
re-exported from `@transistorsoft/background-geolocation-types` via the
package's `index.d.ts`, which is why the build passed. At runtime all five
named imports resolve to `undefined`. The first access — `DesiredAccuracy.High`
inside the config object passed to `BackgroundGeolocation.ready({...})` —
throws `TypeError: Cannot read property 'High' of undefined`, which is exactly
the error the user saw at the bottom of the recording screen on the second
on-walk test. The throw aborts `startWatcher()` before the location listener
can ever fire, which collapses the entire downstream chain (no GPS, no flush,
no mark passes, no `ended_at`).

The user's "stopped temporarily then restarted" auto-restart symptom was a
separate, latent bug uncovered by tapping Stop after the first fix landed.

### Fixes shipped

Three files modified. All scoped to mobile.

1. **`mobile/src/recorder/backgroundGeolocation.ts`** —
   Switched to default-only runtime import and type-only imports from the
   types package. All three enum use sites now go through the static
   properties on the default export (`BackgroundGeolocation.DesiredAccuracy.High`
   etc.). Added an 8-line warning comment above the import block explaining
   the trap so a future dependency upgrade doesn't reintroduce the named-import
   pattern.

2. **`mobile/app/(app)/recording.tsx`** — full rewrite. Four user-visible
   changes:
   - LIVE pill moved into the back-chip row, left-aligned. No longer overlaps
     the status-bar battery icon.
   - `MapFabs` cluster mounted on the top-right with the same compass / layers
     / centre handlers home uses. `windOn` defaults to `true` so the wind
     context is visible immediately on entry to the racing screen.
   - `mapRef` added + a mount effect that calls
     `setTimeout(() => mapRef.current?.fitToRace(selectedRace), 0)` to fit the
     camera to the course. Works around a ref-timing race in `MapCanvas`'s
     internal fit effect that silently fails on screens where `marks` are
     present from the very first render.
   - `zoom={viewport?.zoom}` now passed to `WindBarbLayer`. Previously
     omitted, which defaulted the barb-geometry sizing to z11 and made the
     barbs render at world-scale roughly 16× too large at the user's actual
     z15-ish zoom.

3. **`mobile/src/recorder/useAutoStartRecorder.ts`** — added a module-scoped
   `firedKeys: Set<string>`. The hook lives in `(app)/index.tsx`, which
   remounts after `router.replace("/")`. Without persistence across mounts,
   tapping Stop sent the user into a loop: home remounts → fresh `fired=false`
   → race `start_at` is still in the recent past → setTimeout fires → recorder
   restarts → effect detects `recording=true` → bounces back to `/recording`.
   Module-scope state survives the navigation and the foreground timer now
   no-ops if the key is already in the set. Key change (new race or edited
   `start_at`) clears the prior entry so re-arming after an edit still works.

## Files changed

- `mobile/src/recorder/backgroundGeolocation.ts`
- `mobile/app/(app)/recording.tsx`
- `mobile/src/recorder/useAutoStartRecorder.ts`

## Decisions and rationale

- **Diagnose before patching the user-visible symptoms.** The original three
  observations (notification action button, live track polyline, auto-finish)
  are all real wants, but none of them would have made the user's race
  recoverable last night — there was nothing to act on. Investigating the
  recorder first was the higher-ROI path. The dev-plan additions for the
  three originals are now queued behind device verification.
- **Option A over Option B for the v5 import fix.** Reaching enums through
  the default export (`BackgroundGeolocation.DesiredAccuracy.High`) is more
  verbose at call sites but explicitly matches the v5 README's documented
  pattern and is more robust against the types package being marked
  `devDependency` in a future upgrade.
- **Surgical fixes over refactors.** All three runtime bugs have a structural
  root cause — `MapCanvas` fit-to-marks is genuinely racy with the Camera ref
  attach, and `useAutoStartRecorder` shouldn't have mount-scoped state at all.
  Both were patched at the call site rather than redesigned, to keep the diff
  attributable. Tech-debt items below capture the longer-term fixes.
- **`windOn` defaults to true on `/recording`.** Wind direction is essential
  on-water context; defaulting it off means the user has to tap a FAB they
  may not have memorised yet during the first 30 seconds of a race.
- **No code yet for the original three observations.** Implementing the
  auto-stop hook, `ended_at` PATCH, live actual-track polyline, and
  notification action button before the recorder is verified to be capturing
  on-water risks polishing fixtures we haven't built foundation under.

## Open items

- **Awaiting on-water verification of this build.** EAS preview profile
  rebuild required (`eas build --profile preview --platform android` from
  `mobile/`). Acceptance criteria for the next test: `recorder.lastPoint`
  populates within seconds of pressing Start; `queueLength` rises and drains;
  `error` stays null; wind barbs render at sensible size; course fits on
  entry; LIVE pill clear of the status bar; Stop button actually stops and
  stays stopped.
- **Original three observations.** Hold until verification. Then in this
  order, sized by user impact:
  1. Port `useAutoStopRecorder` from `frontend/src/hooks/` to mobile +
     PATCH `ended_at` on both auto-stop and manual stop. The backend's
     `COALESCE(ended_at, ...)` already protects against clobbering the
     authoritative server value.
  2. Live actual-track polyline as a new `LineLayer` on `MapCanvas` fed by
     `recorder.points`, distinct stroke colour from the planned route. Add
     an optional pulse on `recorder.lastPoint` if time permits.
  3. T-6 notification action button via
     `Notifications.setNotificationCategoryAsync({ identifier: "race-autostart",
     actions: [{ identifier: "START_NOW", buttonTitle: "Start Recording Now",
     opensAppToForeground: true }] })`. Wire `response.actionIdentifier ===
     "START_NOW"` in the existing response listener to invoke the same
     `onFire()` body taps already trigger.

## Technical debt flagged

- **Library-types-lie-about-runtime-exports.** Even with the comment in
  `backgroundGeolocation.ts`, a future upgrade to v6 may reintroduce or
  rearrange the named-export story. Worth a CI check or a runtime
  `assert(typeof BackgroundGeolocation.DesiredAccuracy === "object")` at
  module load to fail loudly on a regression.
- **`MapCanvas` fit-to-marks effect is genuinely racy.** The surgical fix in
  `recording.tsx` works but the underlying effect in `MapCanvas` will keep
  silently failing on any future screen that mounts with marks present.
  Longer-term: move the initial bounds into `Camera`'s declarative
  `defaultSettings.bounds` so the fit is timing-independent.
- **`useAutoStartRecorder` lives one level too low.** Module-scope
  `firedKeys` is a surgical fix; the proper home for the hook is one level
  up — either `RecorderProvider` or `(app)/_layout` — so the hook's own
  state survives navigation without needing module globals. Same applies to
  the `windOn` state we just duplicated on `/recording`: it should probably
  live at the layout level so toggling on home and racing screens stays in
  sync.
- **Pre-race accidental telemetry can poison `started_at`.** The single
  stray point on 2026-05-27 set the race's `started_at` to the planned gun
  time even though no race-window telemetry ever arrived. Worth considering
  a guard in `track_ingest`: only backfill `started_at` if the incoming
  batch's max timestamp is within, say, ±30 minutes of `start_at`.

## Useful commands captured this session

- Confirm what `react-native-background-geolocation` actually exports:
  ```powershell
  Select-String -Path node_modules\react-native-background-geolocation\src\index.js -Pattern "^export"
  ```
- Verify build env on the laptop side before EAS build:
  ```powershell
  cd E:\Personal\Coding\SailLine
  Select-String -Path mobile\.env* -Pattern "EXPO_PUBLIC_API_URL"
  ```
  No match → `mobile/src/api.ts` default (prod Cloud Run) wins, which is
  correct for on-water testing.
- Diagnostic SQL when "race didn't end" recurs:
  ```sql
  SELECT COUNT(*), MIN(recorded_at), MAX(recorded_at)
  FROM track_points WHERE race_id = '<race_id>';
  ```
  If count is 0 or all timestamps are outside the race window → recorder
  never flushed; the auto-stop hook can't fix what isn't there.
