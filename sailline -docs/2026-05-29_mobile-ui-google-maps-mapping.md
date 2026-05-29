# 2026-05-29 — Mobile UI: Google Maps mapping & webapp scope split

**Status: spec — not yet implemented.** Source notes captured from a
Google Maps screenshot (Burnham Harbor pin + place sheet). This doc is
the design contract; implementation will land in phased PRs starting
with Phase 1 (layout swap only).

## 1. Context

SailLine's mobile app already has a Google-Maps-style bottom-sheet
layout (`RaceListSheet`, `RaceDetailSheet`) over a full-bleed
`MapCanvas`, a top-right FAB cluster (`MapFabs`: compass / wind-layer /
locate-me), and the `BetterRouteBanner` auto-route countdown. This
spec aligns the rest of the UI to the patterns the user already trusts
from Google Maps, and locks in the platform split between webapp and
mobile.

## 2. Platform split (locked)

| Platform | Responsibilities |
|---|---|
| **Webapp** | Race setup, pre-race routing calculation, boats & crew management, user profile, post-race analysis. |
| **Mobile** | Everything in webapp *plus* on-water recording, in-race routing/recompute, orientation calibration, in-race AI advice, live telemetry. |

Recording and calibration in the webapp are **hidden now, removed
later**. Gate behind `VITE_RECORDING_ENABLED=false` (default off in
production) so existing users stop seeing the entry points immediately;
delete the code in a single PR once mobile reaches parity.

Affected webapp files (gate now, delete later):

- `frontend/src/hooks/useTrackRecorder.js` + `.test.js`
- `frontend/src/hooks/useAutoStartRecorder.js` + `.test.js`
- `frontend/src/hooks/useAutoStopRecorder.js` + `.test.js`
- `frontend/src/hooks/useHeelGauge.js`
- `frontend/src/hooks/useTelemetryStream.js`
- `frontend/src/sensors/imu.js`, `sensors/orientation.js`
- `frontend/src/SensorDebugView.jsx` (already `?debug=sensors` only)
- `frontend/src/components/PermissionBanner.jsx`
- Recording/calibration UI inside `components/MapView.jsx` and `RaceEditor.jsx`

The `RaceStatsView.jsx` post-race analysis stays — it's webapp-owned.

## 3. Google Maps → SailLine element mapping

Each row maps a Google Maps UI element from the source screenshot to the
SailLine equivalent and its implementation status.

| # | Google Maps element | SailLine equivalent | Existing component | Status |
|---|---|---|---|---|
| 1 | Profile circle (top-right) → side menu | Profile FAB → drawer: Account, Race Setup, Boats, Race History, Mobile Settings, Help, Sign-out | none | **new (Phase 2)** |
| 2 | Default camera on user | Camera follows GPS when no race selected; fits race bbox when one is | `MapCanvas` `fitToRace` | extend |
| 3 | Place title ("Burnham Harbor") | Race name when race selected; nearest waypoint otherwise | `RaceDetailSheet` headerRow | extend (Phase 1) |
| 4 | Car icon + 34 min | Sailboat icon + ETA chip from `routeMeta.total_minutes`; "—" when no route | none | **new (Phase 1)** |
| 5 | Save FAB (bookmark) | Flag race FAB (toggle `race.flagged`) | none | **new (Phase 2, needs migration)** |
| 6 | Share FAB | Pre-race: existing crew-invite path. Post-race: stats share token | partial (crew invites) | extend (Phase 2) |
| 7 | "X" FAB to minimize | Collapses sheet to peek (32%), map gains focus | `BottomSheet` snap points exist | extend (Phase 1) |
| 8 | Directions FAB | Compute / Recompute route — relabel existing button as FAB | `RaceDetailSheet` secondary btn | restyle (Phase 1) |
| 9 | Start FAB | Start recording — relabel existing primary CTA as FAB | `RaceDetailSheet` primaryCta | restyle (Phase 1) |
| 10 | Ask AI FAB | Race-prep chat: "given wind X, how should I sail today?" with race + polar + boat context | none | **new (Phase 2)** |
| 11 | Photos | Post-race photo upload, GCS-backed | none | **new (Phase 4, deferred)** |
| 12 | Overview tab | Weather conditions card: wind, gust, temp, wave | partial (barbs) | extend (Phase 1) |
| 13 | Directory tab | **Tactical Plan** tab — turn-by-turn maneuvers derived from isochrone route legs | none | **new (Phase 3)** |

## 4. Phase 1 — Layout swap only (this is the build target)

**Goal:** ship the visual reorganisation with zero schema changes and no
new backend dependencies. The user gets the Google Maps feel
immediately; new functionality lands in later phases.

### Mobile changes

1. **FAB cluster reorganisation** in `MapFabs.tsx` — add the action FABs
   (Directions = Compute, Start = Recording, X = Minimize) as a second
   bottom-right cluster. Keep compass/layers/locate where they are.
2. **`RaceDetailSheet.tsx` header** — when a race is selected, show
   race name as the title (already does this) but add an ETA chip
   beneath the metadata row: sailboat icon + `formatMinutes(routeMeta.total_minutes)`
   or `—`.
3. **Sheet title fallback** — when no race is selected, `RaceListSheet`
   already shows the list; no change needed.
4. **Minimize behaviour** — bind X FAB to `sheetRef.snapToIndex(0)`
   (32% peek) when sheet is expanded; hide the FAB when already peeked.
5. **Compute/Recompute relabel** — the existing "Compute" secondary
   button stays inside the sheet for in-detail use. The Directions FAB
   on the map calls the same handler — duplicated entry point, not
   duplicated state.
6. **Start FAB** — same pattern as Compute. The in-sheet Start CTA
   stays; the map FAB shadows it.

### Webapp changes

1. Add `VITE_RECORDING_ENABLED` to `frontend/.env.example` and
   `frontend/vite.config.js` exposure list.
2. Wrap all recording/calibration entry points in `AppView.jsx`,
   `MapView.jsx`, `RaceEditor.jsx` with `import.meta.env.VITE_RECORDING_ENABLED === "true"`.
3. Default to `false` in production builds. Local dev can opt in.

### Acceptance criteria (Phase 1)

- Open mobile app cold: map fills screen, FAB cluster top-right, race
  list sheet at peek.
- Select a race: sheet header shows race name + date + ETA chip. Map
  fits to course.
- Tap Directions FAB: compute route runs, banner shows result. Same as
  tapping in-sheet Compute.
- Tap Start FAB: same as in-sheet Start.
- Tap X FAB: sheet snaps to peek; FAB disappears.
- Open webapp in production build: no record/calibrate buttons visible
  anywhere. Stats view still works. Routing still works.

### Out of scope (Phase 1)

- Profile drawer
- Save/flag
- Share (mobile)
- Ask AI
- Tactical Plan tab
- Overview tab
- Photos tab
- Any backend schema change

## 5. Phase 2 — Profile drawer, Save/Share, **Ask AI**

Pulled forward per 2026-05-29 decision. Backend work begins here.

### New backend

- Migration `0xxx_race_flags_and_share.py`:
  - `races.flagged BOOLEAN NOT NULL DEFAULT FALSE`
  - `races.share_token UUID NULL` (nullable; generated on first share)
  - Index on `share_token` for lookup.
- `GET /api/races/shared/{token}` — read-only race view, no auth.
- `POST /api/ai/race-advice` — synchronous endpoint, takes
  `{race_id, question?}`, returns text. Server pulls race + polar +
  forecast + boat spec from existing services and prompts Claude API.
  Pro-tier gated.

### Mobile

- Profile drawer reachable from a top-right circular avatar FAB.
- Save/flag FAB toggles `race.flagged`.
- Share FAB: pre-race opens crew-invite sheet (existing); post-race
  opens system share-sheet with `share_token` URL.
- Ask AI FAB opens a bottom sheet with a text input; pre-fills with
  "Given the wind today, how should I sail this race?" as default
  prompt. Streams response.

### Webapp

- Drawer parity for profile / boats / race history (already exists).
- AI tab on `RaceStatsView` for post-race analysis (separate
  endpoint).

## 6. Phase 3 — Overview & Tactical Plan tabs

- **Overview tab** in the race detail sheet: weather conditions card.
  Pulls from existing `/api/weather` for the race start time and
  course centroid.
- **Tactical Plan tab**: lists each isochrone leg as a maneuver step:
  *"Tack to port at 14:32, sail 048° for 1.2nm"*. Derived from the
  `RouteMeta` waypoint sequence — no new endpoint needed if leg data
  is already returned; add it to the response if not.

## 7. Phase 4 — Photos (deferred)

- Post-race only. GCS bucket `sailline-race-photos` with race-id
  prefix.
- `POST /api/races/{id}/photos` (multipart) + signed-URL fetch.
- Mobile uses `expo-image-picker`.

## 8. Open questions

1. **Ask AI endpoint shape**: sync `POST /api/ai/race-advice` or
   streamed SSE? Sync is simpler; SSE matches the existing route
   notifications pattern.
2. **Share token revocation**: do we need to invalidate share links
   after a race is deleted? (Probably yes — cascade or set to NULL on
   delete.)
3. **Flag scope**: per-user or global on the race row? If a race is
   shared, does each viewer flag independently? **Default: per-user**
   — requires a `user_race_flags(user_id, race_id)` table, not a
   `races.flagged` column. Revisit before Phase 2 migration.
4. **Profile drawer route**: native drawer (`react-navigation`) or
   bottom sheet from a top-right FAB? Native drawer matches Google
   Maps's actual behaviour but adds nav config; sheet is consistent
   with the rest of mobile.

## 9. Webapp deprecation criteria

The webapp recording/calibration code stays in `main` (hidden) until:

- [ ] Mobile recorder validated on water with auto-start, auto-stop, IMU flush, mark passes.
- [ ] Mobile orientation calibration validated on water.
- [ ] Mobile in-race routing recompute parity verified.
- [ ] At least one user (Grayson) has completed a full race on mobile only.

Once all four boxes are checked, a single deletion PR removes the
files listed in §2 and bumps the frontend major version. No grace
period beyond that — the webapp is for setup and post-race from then
on.

## 10. Filed alongside

- Project rule (CLAUDE.md): "Plan before paste."
- Prior session: `2026-05-28_session-recorder-crash-fix.md` (mobile
  recorder still stabilising — Phase 1 should not assume it's
  production-ready).
- Prior session: `2026-05-28_session_barbs-orientation-autoroute.md`
  (orientation + auto-route shipped; this spec assumes both stay).
