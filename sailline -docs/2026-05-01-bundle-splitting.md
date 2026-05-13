# Session Recap — 2026-05-01: Bundle Splitting

**Outcome:** First-paint bundle for the auth-gate path dropped from ~560 KB gzipped to ~86 KB gzipped — an 85% reduction. Cellular dock users no longer download mapbox-gl just to see the login form. Two-file change (`App.jsx`, `AppView.jsx`); no design changes, no behavior changes for authenticated users beyond a one-time chunk fetch on first login.

This is the third summary on 2026-05-01 — the first (`2026-05-01-session-summary.md`) covered the Alembic migration framework, the second (`2026-05-01-preship-ux.md`) covered the map-as-single-pane-of-glass and race start time countdown.

---

## TL;DR

- ✅ `App.jsx` lazy-loads `AppView` via `React.lazy()` + `Suspense`. Auth-gate path no longer pulls mapbox-gl.
- ✅ `AppView.jsx` lazy-loads `RaceEditor` and `RacesListView`. Map-only authenticated sessions don't pay for editor or list code.
- ✅ Build verified — auth-gate first paint is `index` (48.65 KB gz) + `jsx-runtime` shared (36.30 KB gz) + CSS (0.71 KB gz) ≈ **86 KB gz total**.
- ✅ mapbox-gl moves into a separate 474 KB gz chunk (named `latlon-*` because Rollup auto-named the shared chunk after the first module to land in it; cosmetic only).
- ✅ Editor and races list end up in their own small chunks (7 KB and 1.86 KB gz respectively) — editor-only changes now bust 7 KB of cache instead of 560 KB on returning users.

---

## What changed

### Files modified

| File | Change |
|---|---|
| `frontend/src/App.jsx` | `AppView` imported via `lazy()`. Existing dark `var(--night)` splash is reused as the `Suspense` fallback so the chunk-download state visually matches the pre-auth-resolution state — one continuous "still loading" rather than two flashes. |
| `frontend/src/AppView.jsx` | `RaceEditor` and `RacesListView` imported via `lazy()`. Each conditional render wraps in `Suspense` with a small `ViewLoading` fallback (paper background, muted "Loading…" text) that sits inside the existing layer wrapper so it inherits the right z-index. |

### Files explicitly NOT changed

- `MapView` stays eagerly imported by `AppView`. By the time the AppView chunk has loaded, the mapbox-gl cost is already paid — adding another `Suspense` boundary around the always-mounted base layer would only flicker on every login without saving anything.
- `vite.config.js` left alone. Rollup's automatic code splitting at lazy boundaries does the right thing without `manualChunks`.

---

## Why the leverage point was `App.jsx`, not `MapView`

Initial framing was "lazy-load `RaceEditor` and probably `MapView` itself behind the auth gate." But `MapView` is the always-mounted base layer of `AppView` — it renders the moment a user authenticates. Lazy-loading it from inside `AppView` saves nothing real on the auth-gate path (because `AppView` itself was eager) and adds a Suspense flash on every login.

The real leverage is one level up: lazy-load `AppView` from `App.jsx`. That single boundary puts everything authenticated (MapView + mapbox-gl + RaceEditor + RacesListView + the wind barb library) behind the auth gate in one shot. The auth-gate user pays for `AuthView` only.

`RaceEditor` and `RacesListView` are *additionally* lazy because both are behind navigation (menu drawer / button click), so the common "open the app, look at wind on the map" path doesn't pull editor code either. Marginal but real.

---

## Verification

```text
dist/assets/index-*.js              147.06 kB │ gzip:  48.65 kB
dist/assets/jsx-runtime-*.js        120.33 kB │ gzip:  36.30 kB
dist/assets/index-*.css               1.52 kB │ gzip:   0.71 kB
                                                ──────────────
                                    auth-gate ≈ 85.66 kB gz

dist/assets/AppView-*.js             16.80 kB │ gzip:   6.42 kB   (lazy)
dist/assets/RacesListView-*.js        5.25 kB │ gzip:   1.86 kB   (lazy)
dist/assets/RaceEditor-*.js          20.46 kB │ gzip:   7.00 kB   (lazy)
dist/assets/latlon-*.js           1,744.11 kB │ gzip: 474.65 kB   (lazy, mapbox-gl)
dist/assets/latlon-*.css             40.93 kB │ gzip:   5.59 kB   (lazy, mapbox-gl css)
```

The 500 KB chunk-size warning fires on the mapbox chunk. Not actionable — mapbox-gl is the size it is. Suppression skipped intentionally; the warning's location is self-documenting once you know which chunk it's about.

---

## Discussed but deferred — multi-region support

While scoping this session, I asked whether to bundle multi-region support (Chesapeake / SF Bay) in the same pass — partly to set up onboarding for a Chesapeake-area dev. Decided against it. The work doesn't share code paths with bundle splitting, and it's substantially bigger than it looks:

- Worker: `DEFAULT_BBOX` becomes per-region; either a second Cloud Run Job per region or one job that loops them, writing `weather:{source}:{region}:latest` (the long-deferred Redis key migration finally has to happen)
- Router: `chesapeake` already commented out in the `REGIONS` registry, but the read key now needs the region segment
- Frontend: region picker in the menu drawer + a `home_waters` field on the user profile (= schema migration #4)
- Wave data is a separate provider for Chesapeake — Great Lakes WW3 doesn't cover it. Likely NOAA NWPS or the East Coast WW3 grid.

Probably a week's work touching schema, worker, API, and UI. For onboarding the new dev, they don't need their region live to start contributing — they can develop UI/feature work against `great_lakes` data. Real-race usage in their home waters is weeks out anyway.

---

## Operational notes (additions)

- **Lazy `Suspense` fallbacks won't appear in `npm run dev`.** Vite's dev server resolves dynamic imports immediately. To eyeball the fallbacks, throttle DevTools to Slow 3G and reload — once is enough to confirm the splash + ViewLoading both feel right.
- **First authenticated session pays a one-time chunk download** (~480 KB gz for the mapbox chunk). After that it's cached and subsequent loads are instant. The dark `var(--night)` splash covers this gap and looks identical to the auth-resolving splash that already happens.
- **Returning-user cache wins.** Content-hashed chunks mean editor-only changes invalidate the 7 KB editor chunk, not the 480 KB mapbox chunk. Smaller deploys = less cellular re-download for users who hit the app between releases.
- **Rollup chunk naming is by first-module-in.** The mapbox chunk is named `latlon-*` because `lib/latlon.js` happened to be the first shared module Rollup placed there. Cosmetic. If we ever care to fix it, a 3-line `manualChunks: { 'mapbox': ['mapbox-gl'] }` in `vite.config.js` would do it.

---

## Open items / next session

Inheriting from `2026-05-01-preship-ux.md`'s next-session list, with bundle splitting now closed:

1. **`infra/schema.sql` ownership transfer** for `user_profiles` and `race_sessions`. Prod is fine; gap only affects fresh-DB bootstrap. Two-line append to `schema.sql`, then commit and push.
2. **`pythonpath = .` in `backend/pytest.ini`** — small QoL fix so `pytest` works without the `python -m` prefix.
3. ~~Bundle splitting~~ ✅ shipped this session.
4. **Long-distance course presets** in `morfCourses.js` (Zimmer, Skipper's Club, Hammond, etc.). Mark library supports them; just need the entries.
5. **Week 2 weather pipeline continuation.** Independent of UI work; can interleave.
6. **Multi-region support** (new). Documented above. Trigger: when the Chesapeake dev is ready to run real races against their home waters, or when a second region's worth of users justify the lift — whichever comes first.

---

## Pre-ship feature backlog

Both items shipped earlier today (per `2026-05-01-preship-ux.md`):

- ~~Map as single pane of glass after save~~ ✅
- ~~Race date / class start time fields with countdown~~ ✅

Backlog is empty. Pre-ship UX is in a good state.
