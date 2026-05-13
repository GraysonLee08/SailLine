# Week 2, Step 5 — Frontend Weather Consumption

**Status:** Shipped (barbs-only).
**Date completed:** April 29, 2026.

## What we built

A full-screen Mapbox map that consumes the `/api/weather` endpoint and renders the HRRR wind field as accurate, modern wind barbs. Replaces the previous split-pane `AppView` (hero + welcome stub) with a map-first product surface gated behind a slide-in menu drawer.

### Files delivered

| File | Purpose |
|---|---|
| `frontend/src/hooks/useWeather.js` | Fetches `/api/weather`, refetches every 5 min, caches ETag in a ref, sends `If-None-Match` on refetch, handles 304s by keeping existing data. Exposes `{data, referenceTime, validTime, ageMinutes, loading, error}`. |
| `frontend/src/hooks/useGeolocation.js` | One-shot `navigator.geolocation.getCurrentPosition` with 8s timeout. Returns `{position: {lat, lon, accuracy}, error, loading}`. |
| `frontend/src/lib/windBarb.js` | `uvToSpeedDir(u, v)` converts m/s components → knots + "from" direction. `generateBarbImages()` returns 14 pre-generated SVG data URLs (one per 5kt bucket, 0–65kt). Modern aesthetic: thin slate-800 strokes, rounded caps, NH convention. |
| `frontend/src/components/MapView.jsx` | Mapbox map (`light-v11` style), Lake Michigan default center, GPS recenter via `flyTo`. Subsamples HRRR every 4th point (~1200 barbs). Symbol layer uses `icon-rotate` from `dir`, `icon-image` via `concat` expression. |
| `frontend/src/AppView.jsx` | Full-screen map shell, hamburger button (top-right), slide-in drawer (340px) with user info, tier chip, navigation placeholders, sign-out. Closes on backdrop click or ESC. |

### Dependency changes

- `@vitejs/plugin-react` upgraded `^4.3.4` → `^6.0.0` (required for Vite 8 / Mapbox compatibility).
- `mapbox-gl` added.
- New env var: `VITE_MAPBOX_TOKEN` in `frontend/.env.local`.

### Verification

Barbs were verified accurate against Windy.com side-by-side at multiple locations across the Great Lakes domain.

## What we tried and abandoned

A significant chunk of this step was spent attempting an animated wind particle visualization (the Windy-style flowing streaks). Two paths were investigated and shelved.

### Path 1: Custom WebGL particle layer

Adapted from `mapbox/webgl-wind` (ISC license) as a Mapbox `CustomLayerInterface`. Architecture:
- Wind data uploaded as RGBA texture, u/v normalized to ±30 m/s in R/G channels
- Particle state in ping-pong textures, 16-bit per axis
- Trail compositor with screen-sized FBO and fade pass
- Position projection: bbox uv → lon/lat → mercator → Mapbox MVP

**Result:** Geometry verified correct via constant-velocity test (particles all flowed cleanly to the ENE). With real wind data, particles consistently produced false vortex/convergence patterns near Milwaukee/Chicago that did not exist in the actual data (verified against barbs and Windy from the same JSON payload). The bug was never isolated. Theories ruled out without finding root cause:
- Texture y-flip / `UNPACK_FLIP_Y_WEBGL` state
- Sampler coordinate convention
- Trail compositor framebuffer mismatch
- Sign errors in velocity offset

### Path 2: Mapbox official `raster-particle` layer

Researched as a replacement for the custom layer. Architecture would have required:
- Backend pipeline rebuild: GRIB2 → Mapbox Tiling Service (MTS) raster-array tilesets
- New worker dependency: `mapbox-tilesets` CLI
- Mapbox token with `tilesets:write` scope
- Frontend collapses to ~15 lines of `raster-array` source + `raster-particle` layer

**Cost analysis killed it.** Per Mapbox MTS pricing (Tileset Processing 10m, max zoom 6–10):
- HRRR great_lakes hourly = 720 publishes/mo ≈ **$5,100/mo**
- HRRR + GFS great_lakes hourly = 840 publishes/mo ≈ **$6,100/mo**
- All planned regions × both sources ≈ **$26,000/mo**

Not viable for solo-dev pre-revenue.

### Decision

Ship v1 with barbs only. Most desktop weather routers (Expedition, qtVlm) use barbs as primary visualization — racers know how to read them.

## Open items / v2 considerations

### Carried forward from Step 4 (still outstanding)

- **Worker test coverage is zero.** Need `tests/test_weather_ingest.py` covering the GRIB2 → Redis/GCS path.
- **No monitoring/alerting on Cloud Run Job failures.** Worker can silently fail and we'd never know until barbs go stale.
- **Redis key scheme.** When the second region ships, the worker needs to write `weather:{source}:{region}:latest` instead of the current single-region key.

### New from Step 5

- **Wind particle visualization (deferred).** Two paths to revisit post-launch:
  - Resume custom WebGL layer debugging in a fresh, deliberate session. Bug is likely a 1-line fix once isolated. Add diagnostic instrumentation (rendering wind texture directly, particle trace logs) before guessing.
  - Reconsider `raster-particle` if revenue makes the cost proportional. At ~$5k/mo for one source/region, this needs ~50–100 paying users at Pro tier to break even on this feature alone.
- **`useGeolocation` is one-shot only.** Future AIS integration (Week 7) will need a position hook that updates continuously from boat hardware, not just browser GPS.
- **Menu drawer items are all placeholders.** Race setup, Boat profile, Home waters, Settings, Help — all wired but disabled. Each becomes a real screen in Weeks 3–10.
- **No region selector.** Map is hardcoded to `great_lakes` / `hrrr`. When a second region ships (likely Chesapeake or San Francisco Bay), need a region picker — probably tied to the user's "home waters" profile setting.
- **No source picker.** HRRR only currently. GFS is fetched and cached server-side but the frontend doesn't let users switch. Add toggle when forecast horizon becomes a feature (HRRR is 18h, GFS is 384h).
- **Subsample is fixed.** `SUBSAMPLE = 4` works at zoom 6–9 but gets sparse when zoomed in tight. Could be made zoom-adaptive: `Math.max(1, Math.round(8 - zoom))` or similar.
- **Barbs don't fade in.** New data swaps in instantly. Smooth crossfade between forecast frames would be nice for the eventual time-scrubbing UI.
- **No legend.** Knot scale + barb convention (full flag = 50, full barb = 10, half barb = 5) should be visible somewhere. Probably bottom-right corner, dismissable.
- **GPS zoom override is hardcoded to 9.** When debugging, easy to forget the GPS `flyTo` will overwrite the default zoom. Consider exposing as a constant or making the GPS recenter user-triggered (a "locate me" button) instead of automatic.

### Architecture debt to track

- The Mapbox token is unrestricted (no URL or referrer limits set). Before public launch, lock it down to `claude.ai`/production domain to prevent quota theft.
- `VITE_MAPBOX_TOKEN` is committed to `.env.local` only. Production deployment will need this in Firebase Hosting env or build-time secret.
- `MapView.jsx` styling is inline. Fine for now; if the file grows past ~300 lines, extract to CSS modules.

## Next up

Week 3 — likely race setup or boat profile, depending on which unlocks more downstream work. Both are gated on having a settled data model for boats/races, so v2 weather features wait their turn.
