# Session — CONUS + Venue Architecture

**Date:** May 1–2, 2026.
**Status:** Code shipped, tests passing 33/33, rollout in progress (CONUS HRRR ingest verified 200 in production).

## Problem

User flagged that Myrtle Beach (~33.7°N) showed no wind barbs. Diagnosis: a coverage gap between the `chesapeake` (>36.5°N) and `florida` (<26.5°N) regional bboxes built in the previous multi-region rollout. The discrete-bbox model itself was the wrong shape — every uncovered sailing area was a future bug.

The deeper issue: the multi-region architecture only ever serves one bbox at a time. A user in Chicago panning over to Florida sees no wind because they're loading `great_lakes`, not `florida`. Per-region grids optimized download size at the cost of "the map works everywhere."

## What we built

A two-layer wind rendering system: an always-on base layer covering the full CONUS plus Hawaii, with high-resolution venue overlays at 15 popular sailing areas that activate when the user zooms in.

| Layer | Source | Resolution | Coverage | Purpose |
|---|---|---|---|---|
| Base CONUS | HRRR | 0.10° (~11 km) | Full CONUS | Always on; passage/distance racing |
| Synoptic CONUS | GFS | 0.25° (native) | Full CONUS | Multi-day forecasts (later feature) |
| Hawaii | GFS | 0.25° (native) | Hawaii bbox | Outside HRRR's CONUS domain |
| Venue overlays | HRRR | 0.027° (native ~3 km) | 15 venue bboxes | Buoy-racing tactical detail |

15 venues at native HRRR: chicago, milwaukee, detroit, cleveland, sf_bay, long_beach, san_diego, puget_sound (Bellingham→Tacoma), annapolis, newport_ri, buzzards_bay, marblehead, charleston, biscayne_bay, corpus_christi.

Total 17 regions, 18 (source, region) pairs, 18 ingest jobs. Replaces the previous 19-job setup. Cost roughly flat (~$35–40/mo).

### Files delivered

| File | Change | Purpose |
|---|---|---|
| `backend/app/regions.py` | NEW | Single source of truth: `Region` dataclass with `kind` ("base" or "venue"), bbox, and per-source resolution map. Helpers: `base_region_for_point()`, `venue_for_point()`, `all_pairs()`. |
| `backend/workers/weather_ingest.py` | REPLACE | `--region` is now required; resolution is looked up per-region via `region.resolution_for(source)`. Source dataclass loses `target_resolution_deg`. |
| `backend/app/routers/weather.py` | REPLACE | Region/source validation flows through the registry's `region.sources`. No legacy fallback (Step 5 cleanup is absorbed into this commit). |
| `backend/tests/test_weather_router.py` | REPLACE | Tests updated for `conus`, `hawaii`, `sf_bay`. New: `test_gfs_on_venue_returns_400` (venues are HRRR-only). |
| `backend/tests/test_weather_ingest.py` | REPLACE | New synthetic-grid helpers per region kind. New tests assert the per-region resolution propagates correctly to `parse_grib_to_wind_grid` (0.10° for CONUS HRRR, 0.027° for venue HRRR). |
| `backend/tests/test_weather_ingest_live.py` | REPLACE | Live NOAA smoke test gated behind `RUN_REAL_NOAA_TESTS=1`. New: venue native-resolution check (asserts shape ≥ 25×20 for sf_bay). |
| `frontend/src/lib/regions.js` | REPLACE | Mirror of backend with all 17 regions, `kind` field, `VENUE_ZOOM_THRESHOLD=11`, helpers: `baseRegionForPoint`, `venueForPoint`, `marksCentroid`, `regionCenter`. |
| `frontend/src/hooks/useRegion.js` | REPLACE | Returns base region only (conus or hawaii). Validates persisted localStorage value is `kind="base"` so older builds with venue names get silently ignored. |
| `frontend/src/hooks/useWeather.js` | REPLACE | Accepts `region=null` to skip fetching. Used by the venue overlay layer in MapView (React doesn't allow conditional hooks; null-arg is the supported pattern). |
| `frontend/src/components/MapView.jsx` | REPLACE | Two `useWeather` calls: base (always) + venue (conditional via null). Two Mapbox sources/layers: `wind-base`, `wind-venue`. `computeFeatures(map, weather, excludeBbox)` skips points inside the venue bbox when computing base features so we don't double-render at the boundary. |
| `docs/conus-migration.md` | NEW | Full rollout runbook: tear down 19 old jobs/triggers, create 18 new jobs (CONUS HRRR gets 4Gi/2 CPU/20m due to ~1.9M-point Delaunay regrid; rest 1Gi/1CPU/10m), first-run executions, schedule creation. |

### Verified

- 33 / 33 backend tests passing locally on Windows + Python 3.13 + venv.
- Registry sanity test: 17 regions, 18 (source, region) pairs, point detection working in all directions (Chicago→`conus` base + `chicago` venue; Honolulu→`hawaii`; London→`None`; Myrtle Beach venue→`None`, but base CONUS now covers it).
- Production: CONUS HRRR first execution `sailline-ingest-hrrr-conus-8jvj5` completed cleanly. `/api/weather?region=conus&source=hrrr` returns 200.

## Architectural decisions worth remembering

- **Two-layer rendering, not one big grid.** Considered (and rejected) a single full-CONUS grid at 0.05° (~3.5 MB everywhere) for simpler architecture. Picked the layered approach because the marginal accuracy from 5 km → 3 km matters specifically for buoy racing where users *will* notice; everywhere else, base CONUS at 0.10° (~1 MB) is plenty and ETag-cached anyway.
- **Zoom threshold = 11.** ~50 nm visible viewport. Below that, the eye can't tell 0.10° from 0.027° barbs apart anyway, so it's pointless to pay the bandwidth.
- **Both layers always rendered.** The base layer's `computeFeatures` skips points inside the active venue's bbox so we don't render two barb densities on top of each other. Cleaner visual transition than zoom-snapping between resolutions.
- **HRRR-only at venues, GFS only at CONUS + Hawaii.** GFS native is 0.25° (~28 km) — at venue scale that's 3 grid points across SF Bay. Pointless. Saved 8 jobs by dropping GFS at venues.
- **Per-source resolution lives on `Region`, not `Source`.** This was the key API change. CONUS has both HRRR @ 0.10° and GFS @ 0.25°; venues have HRRR @ 0.027°. The Source dataclass now carries only the things that are intrinsic to NOAA's release schedule (cycle/lag/TTL); resolution is per (source, region) and looked up via `region.resolution_for(source)`.
- **Venue selection is viewport-driven, not user-identity-driven.** A Chicago sailor zooming into SF Bay should get high-res SF Bay. So `useRegion` only handles base region detection; MapView itself reads viewport center on `moveend` and decides whether to load a venue overlay. Keeps `useRegion` simple and avoids a circular dependency between hook and map.
- **localStorage validation: kind="base" required.** Previous build wrote venue names like `chicago` into `sailline.region`. New build silently ignores anything that's not a base region, falling back to detection. No migration needed; bad keys age out the next time GPS or IP geolocation runs.
- **B2 dynamic viewport tiles deferred.** Discussed at length — that's the Windy/PredictWind approach for true global scaling. Estimated 2–3 weeks of focused work plus $50–150/mo CDN before a single user. Tabled until there's a real global-user signal. The current API shape (`?region=X` returning a single grid) doesn't paint us into a corner; the barb computation in `MapView.computeFeatures` already iterates the visible viewport, so a future migration mostly changes *what we request*, not *how we render*.
- **ECMWF deferred.** Same logic — current users are US-focused, GFS is free, ECMWF integration is a separate ingest pipeline. Revisit when global users arrive.

## Bugs hit and fixed

1. **Stale region resolution.** Initial draft of `weather_ingest.py` still hardcoded `target_resolution_deg = 0.10` for HRRR. Caught by writing the new `test_ingest_passes_per_region_resolution_for_venue_hrrr` test, which asserted the value reaching `parse_grib_to_wind_grid` was 0.027°. Fix: thread resolution through `region.resolution_for(source_name)` everywhere.
2. **Frozen dataclass + mutable dict.** First version of `Region` had `resolutions: dict[str, float]`. Frozen dataclasses can't have dicts (not hashable). Fix: changed to `source_resolutions: tuple[tuple[str, float], ...]` with a `resolution_for()` lookup method. Stays hashable; lookup is O(n) over typically 1–2 entries which is fine.
3. **Bash placeholder confusion in runbook.** `gcloud run jobs executions logs read <execution-id>` from the runbook ran literally; bash treated `<execution-id>` as a stdin redirect and errored. Real syntax is `gcloud beta run jobs executions logs read sailline-ingest-hrrr-conus-8jvj5 --region=us-central1`. Worth flagging in the runbook for future sessions.

## Open items

### Carried forward

- **Worker-side Cloud Run Jobs OOM/timeout monitoring.** No alerting yet on `run.googleapis.com/job/completed_execution_count{result="failed"}`. Worker can silently fail and we'd never know until barbs go stale. ~5 minute task; haven't done it.
- **Stale Redis/GCS cleanup.** Old `weather:hrrr:great_lakes:latest` and friends will TTL out within their cycle window. GCS objects under `gs://sailline-weather/{hrrr,gfs}/great_lakes/...` etc. are still there; lifecycle policy can clean them eventually.
- **"Home waters" menu wiring.** Drawer item still a placeholder. Becomes wireable once boat-profile screen exists (Week 4–5).
- **`useGeolocation` is one-shot.** Future AIS integration (Week 7) needs a continuous position hook reading boat hardware, not just browser GPS.

### New from this session

- **CONUS HRRR memory headroom.** First execution at 4Gi / 2 CPU / 20m succeeded, but we have no margin data on how close it ran. If a future HRRR cycle has dense data (e.g. severe convective weather over the Plains), the Delaunay tri may push memory higher. Worth monitoring the next 3–5 cycles. If anything trips, bump to 8Gi / 30m or add source-side subsampling in `_regrid_curvilinear`.
- **No venue activation indicator in the UI.** When the venue overlay activates at zoom 11, there's no visible label saying "now showing native 3km HRRR." Decision was to keep the UI uncluttered (the map *is* the indicator — denser barbs = venue active). Worth revisiting if users get confused about why barb density changes during zoom.
- **Tactical-track racing accuracy.** Even at 0.027° native HRRR, we're still consuming the GFS-NDFD pipeline output, not running our own mesoscale forecast. Pro-tier apps doing real America's Cup / Olympic-track work would want WRF runs at sub-1km — that's a category we're not in and won't pretend to be.
- **B2 (dynamic viewport tiles) on the watchlist.** Still the right move when we have global users. Tracking in this doc so the future-self decision point is documented: when international users start filing "no wind" tickets, that's the signal.

## Net result

The Myrtle Beach problem (and the entire class of "wind disappears when I pan" bugs) is gone. Buoy racers in 15 named venues get native-resolution HRRR. The architecture is one ingest pipeline with a per-region resolution knob, not two separate stacks. And the future global-tile rewrite, when it eventually happens, mostly changes the request shape, not the renderer.
