# 2026-05-29 — Mobile UI Phase 1 + platform split locked

**Status: shipped to local repo, not yet built on-device or deployed.**
All edits are in `main`'s working tree; no commits yet. Acceptance still
needs an EAS dev-client build for the mobile FAB cluster and a Windows
`npm run dev` smoke for the webapp gate.

## What we worked on

Two locked decisions plus the first build phase against them:

1. **Spec written** — `2026-05-29_mobile-ui-google-maps-mapping.md` —
   maps the 13 numbered observations from the user's annotated Google
   Maps screenshot to SailLine equivalents, locks the webapp/mobile
   platform split, and phases the work.
2. **Phase 1 implementation** — Google-Maps-style action FAB cluster on
   mobile, ETA chip in the race detail sheet header, and the
   `VITE_RECORDING_ENABLED` gate hiding the webapp's recording and
   calibration surfaces (default off in production).

## Files changed

### New
- `sailline -docs/2026-05-29_mobile-ui-google-maps-mapping.md` — the spec.
- `mobile/src/components/MapActionFabs.tsx` — bottom-right FAB cluster.
  Three FABs in order: Directions (Compute / Recompute, shows
  `ActivityIndicator` when `routeLoading`), Start (recording — hidden
  once `recording === true`), Minimize (collapses the detail sheet,
  only visible when `sheetExpanded === true`). Theme-token styling
  matching the existing top-right `MapFabs`. Bottom offset =
  `insets.bottom + 296` so the cluster floats above the 32% peek
  snap of the race detail sheet.
- `frontend/src/lib/featureFlags.js` — single export
  `RECORDING_ENABLED = import.meta.env.VITE_RECORDING_ENABLED === "true"`.

### Modified
- `mobile/src/components/RaceDetailSheet.tsx` — converted to
  `forwardRef`, exposes `RaceDetailSheetHandle = { snapToPeek() }`,
  new `onExpandedChange?: (expanded: boolean) => void` prop wired
  through `BottomSheet.onChange`. ETA chip added to the header block:
  boat icon + `formatMinutes(routeMeta.total_minutes)` (or `—`) +
  "est. race time" caption. New `etaChip` style.
- `mobile/app/(app)/index.tsx` — imports `MapActionFabs` + the
  `RaceDetailSheetHandle` type, holds `detailSheetRef` + a
  `sheetExpanded` state, renders `<MapActionFabs>` between `MapFabs`
  and `RaceDetailSheet` only when a race is selected, threads
  `ref={detailSheetRef}` + `onExpandedChange={setSheetExpanded}`.
- `frontend/.env.example` — adds `VITE_RECORDING_ENABLED=false` with
  a pointer comment to the spec.
- `frontend/src/components/MapView.jsx` — imports `RECORDING_ENABLED`,
  gates `<PermissionBanner>`, the auto-recording-armed hint, the
  auto-stop hint, the queue/error banners, the calibration row, the
  calibration status, the orientation-denied banner, the live heel
  readout, and the Record button. Compute/Recompute, RouteStatus,
  Edit/Clear, the BetterRouteBanner, and the countdown all stay
  visible because routing + race metadata stay webapp-owned.
- `frontend/src/RaceEditor.jsx` — imports `RECORDING_ENABLED`, gates
  the "Auto-start recording 5 min before gun" checkbox in the editor
  sidebar. The `autoStartEnabled` state still POSTs to the API so
  existing race rows preserve their value — only the checkbox is hidden.
- `frontend/src/AppView.jsx` — imports `RECORDING_ENABLED`, the
  `?debug=sensors` short-circuit now also requires the flag.
- `sailline -docs/Development plan.docx` — section "Mobile UI
  alignment: Google Maps mapping (2026-05-29)" appended earlier this
  session with the spec; one more addendum at end of session noting
  what actually shipped.

## Decisions and rationale

**Pragmatic gate over strict gate (webapp).** Hooks like
`useTrackRecorder`, `useAutoStartRecorder`, `useHeelGauge` stay mounted;
only their visible surfaces are gated. Rules of Hooks rules out
conditional hook calls. Net effect: dead-but-mounted recorder code in
prod, deleted in a single PR once mobile reaches parity. Trades a small
amount of background CPU (the IMU/geolocation listeners can still
attach in dev tools / DevTools console) for a 5-line diff per file
instead of a hook-rewrite across five files.

**Single-source `featureFlags.js` module.** `import.meta.env.VITE_*`
inline reads work, but a central module gives one place to find every
flag and lets us tree-shake when a flag is statically `false`. The
constant is the import, not a function — Vite's dead-code elimination
collapses `RECORDING_ENABLED && (...)` to nothing in production builds
when the env var is `"false"`.

**Action FAB cluster as a sibling of MapFabs, not an extension.**
`MapFabs` (top-right) holds map-state controls — compass / wind
layer / locate-me — that are always relevant. The new action cluster
(bottom-right) holds race-action FABs that only matter when a race is
selected, and is conditionally rendered as such. Splitting them keeps
each component's prop surface small and matches the Google Maps
pattern where viewport controls and place-action controls live in
different places.

**Minimize FAB visible only when expanded.** Matches Google Maps:
the X on the place sheet only appears when the sheet is open. Saves
a redundant control at the peek snap.

**ETA chip always visible, shows `—` until route computed.** The
visual affordance is the goal — sailors should see *where* the ETA
will appear before they tap Compute. An empty slot reads as a
broken layout; a placeholder reads as an action they haven't taken.

**Spinnaker checkbox in RaceEditor uses `styles.autoStartRow` but
stays.** Inherited naming from before recording moved — the row
isn't recording-related. Only the actual auto-start checkbox was
gated. Worth a future style-key rename for clarity.

## Open items and next steps

**Verification gap.** Webapp changes need a Windows `npm run dev` smoke
plus `npm test` (Vitest can't run in this sandbox — bus error). Mobile
changes need an EAS dev-client build to verify on-device. Acceptance
criteria from the spec §4: open cold → map + race list peek + top-right
FABs visible; select race → header shows race name + ETA chip; tap
Directions → compute fires same handler as in-sheet button; tap Start →
records; tap X → peek; webapp prod build → no record/calibrate UI
anywhere, routing + stats still work.

**Phase 2 prerequisites.** Before the next phase ships, two open
questions need answers (captured in spec §8): Ask AI endpoint shape
(sync POST vs. SSE) and flag scope (per-user `user_race_flags` table
vs. global `races.flagged` column). The dev plan flags both as Phase 2
blockers.

**Mobile recorder un-finished work.** The deletion gate in §9 of the
spec lists four required parity items before the webapp deletion PR
can ship: on-water validation of mobile auto-stop, IMU flush in the
recorder, in-race recompute parity, one mobile-only full race. None
of those moved this session — Phase 1 is layout-only by design.

## Tech debt flagged (2026-05-29)

**`VITE_RECORDING_ENABLED` gate is itself debt.** The cleanest end
state is "no recording code in the webapp at all." Until the deletion
PR ships, we have dead-but-mounted hook calls and three component
files threaded with flag checks. Acceptable cost for an interim — but
the gate isn't the goal.

**Hook calls stay mounted in webapp even with flag off.** Pragmatic
choice (above), but means the geolocation/IMU listeners may still
attach to dev builds. Not a prod concern but worth knowing during
debugging.

**`styles.autoStartRow` reused for the spinnaker checkbox.** Misleading
name now that recording is gone. Rename to `styles.checkboxRow` or
similar when the auto-start UI is deleted entirely.

**Bottom offset of `MapActionFabs` is a magic number.** `insets.bottom
+ 296` was sized to sit above the 32% peek snap on a typical phone
(~850px tall). Will be slightly off on tablets and very small devices.
Worth deriving from `Dimensions.get("window").height * 0.32` after the
first round of on-device feedback.

## Acceptance criteria (re-stated for the next session)

Phase 1 is "shipped" when:

- [ ] Webapp prod build under `VITE_RECORDING_ENABLED=false` shows no
      Record button, no Phone-axis pills, no Zero button, no
      auto-recording-armed hint, no heel readout. Routing controls,
      stats view, and race editor (sans the auto-start checkbox) all
      still work.
- [ ] Mobile dev-client build shows the new bottom-right FAB cluster
      when a race is selected. Directions FAB triggers compute; Start
      FAB triggers recording; Minimize FAB only appears when the
      sheet is at 75% and collapses it to 32%.
- [ ] ETA chip shows "—" before route compute and the formatted
      total time after.

Session detail: this file. Spec: 2026-05-29_mobile-ui-google-maps-mapping.md.
