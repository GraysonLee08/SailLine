# Session — ENC Venue Hazards + Segment-Aware Navigability

**Date:** May 6, 2026.
**Status:** Shipped. Navy Pier breakwall and Northerly Island are now correctly avoided. Routes wrap around harbor structures cleanly.

## Problem

Test 2 (Waukegan → Chicago delivery) was producing a route that cut straight through the Navy Pier breakwall and through Northerly Island. The 425 forecast-availability bug from the previous session was fixed, so routes were computing — they just weren't avoiding obvious hazards.

Diagnosis required peeling back several independent issues stacked on top of each other:

1. ENC ingest was using stale layer IDs from `enc_general` and silently returning 6 LNDARE features for the entire CONUS (mostly point-layer garbage). Hazards file on GCS was 610 KB instead of the multi-MB it should have been.
2. The harbour-scale ENC service (`enc_harbour`) — where breakwalls actually live as polygons — wasn't being used at all. CONUS-wide ingest at 1:600k–1:1.5M scale doesn't include venue features that are below that scale floor.
3. `make_navigable_predicate` only loaded the base region's hazard index. Even with venue charts ingested, routing wouldn't see them.
4. The isochrone engine only checks `is_navigable` at stride **endpoints** (every ~770 m at 5 kt). A 20 m wide breakwall has only ~20% chance of being detected by sparse point sampling, regardless of polygon precision.

## What we built

A four-layer fix that moves chart hazards from "loaded but incomplete" to "loaded, venue-aware, and exact line-vs-polygon intersection."

### Layer 1: Correct ENC ingest with two services

`backend/workers/enc_ingest.py` — REPLACE.

- Auto-picks service based on `region.kind`: base regions hit `enc_general` (1:600k–1:1.5M), venue regions hit `enc_harbour` (1:5k–1:50k).
- Layer ID tables updated against the live MapServer manifest. Old IDs were pointing at point-only and group layers, returning either 0 features or 400 errors. New IDs point at the actual polygon layers.
- Auto-chunks the bbox into 2×2 quadrants on timeout, recursively, with per-feature dedup by `OBJECTID`. Fixes ENC Direct's 90 s response-limit timeouts on dense harbour layers like LNDARE and SLCONS.
- Response-body logging on JSON parse failures (was needed during diagnosis; left in for future drift detection).
- Timeout bumped 90 s → 180 s.

Layer table for the harbour service includes `SLCONS` (shoreline construction = breakwalls/jetties/piers) — the layer that catches Navy Pier specifically. Total venue ingest after RESARE filter:

| Layer | Chicago count |
|---|---|
| SLCONS | 24 |
| LNDARE | 72 |
| OBSTRN | 13 |
| WRECKS | 0 |
| CTNARE | 16 |
| MIPARE | 2 |
| DYKCON | 0 |
| FSHFAC | 0 |

127 polygons total. 1.1 MB GeoJSON on GCS.

### Layer 2: Venue-aware navigability

`backend/app/services/routing/navigability.py` — REPLACE.
`backend/app/routers/routing.py` — REPLACE.
`backend/workers/route_recompute.py` — REPLACE.

`make_navigable_predicate` takes an optional `venue` argument and loads BOTH the base region's index and the venue's index when set. A point/segment is hazardous if it touches any polygon from either index. The two scales complement each other: base catches things outside the venue bbox, venue catches things too small to appear at base scale.

Region resolution at the routing layer changed shape. `_resolve_region(marks)` now returns `(base_region, venue_or_None)`. `compute_route` threads `venue` through to the predicate. `route_recompute._recompute_one` uses the same resolver so background "better route" alerts use the same hazard set as the user-facing endpoint — alerts that would route through breakwalls would surface as alerts the user immediately distrusts.

`venue` is folded into the route cache key, so a venue re-ingest naturally invalidates that venue's cached routes without touching base-region routes. `ENGINE_VERSION` was bumped through several values during the session as code shape changed; current is `v9-line-intersect` (or whatever string is in routing.py — bumping invalidates all route cache entries globally).

### Layer 3: Exact line-vs-polygon hazard checks

`backend/app/services/charts/__init__.py` — REPLACE.

`HazardIndex` gains a new `crosses_line(lat1, lon1, lat2, lon2)` method that uses `LineString.intersects(Polygon)` after STRtree-narrowing the candidate set. Exact intersection — catches any polygon the segment touches, regardless of polygon thickness. The point-based `intersects(lat, lon)` is kept for spot checks; both methods coexist.

This is the fix that actually solved the breakwall problem. Point sampling at 100 m intervals has a ~20 % detection rate on a 20 m wide perpendicular obstacle (you need 8 samples × 20m / 770m ≈ 21% by birthday math). Line intersection has 100% detection rate and the cost is one tree query per segment.

### Layer 4: Segment-aware engine

`backend/app/services/routing/isochrone.py` — REPLACE.

The engine no longer only checks `is_navigable(new_lat, new_lon)` at the endpoint. It calls `_segment_check(parent.lat, parent.lon, new_lat, new_lon, is_navigable)`, which prefers `is_navigable.segment(...)` if the predicate exposes it (production case — the predicate from `make_navigable_predicate` does), and falls back to per-point sampling at 100 m intervals for legacy callers (tests with hand-rolled lambdas, the standalone CLI script).

The final approach to the finish mark is also segment-checked. Without this, you can have a perfect route that ends with a final dash through Northerly Island because the engine considered the reached node "in finish radius" without verifying the line from there to the actual mark.

## Files delivered

| File | Change | Purpose |
|---|---|---|
| `backend/workers/enc_ingest.py` | REPLACE | Two-service ingest, correct layer IDs, auto-chunking on timeout |
| `backend/app/services/charts/__init__.py` | REPLACE | New `crosses_line` method on HazardIndex |
| `backend/app/services/routing/navigability.py` | REPLACE | Venue + segment predicate via `.segment` attribute |
| `backend/app/services/routing/isochrone.py` | REPLACE | Engine uses `.segment` predicate; final-leg validation |
| `backend/app/routers/routing.py` | REPLACE | `_resolve_region` returns (base, venue); ENGINE_VERSION bumped |
| `backend/workers/route_recompute.py` | REPLACE | Same venue resolution as user-facing endpoint |
| `backend/tests/test_navigability_venue.py` | NEW | Tests for two-index merging |

## Operational notes

- **Cloud Run filters INFO-level logs from app loggers.** Only WARNING+ comes through. The `navigability for region=...` line was bumped to `log.warning` for visibility. Drop back to INFO when proper structured logging is configured.
- **Cache invalidation during dev.** The natural cache key already covers the cases that matter in production (forecast cycle changes invalidate within an hour). For dev iteration, bump `ENGINE_VERSION` in `routing.py` — that globally invalidates all route caches in one push.
- **Compute time impact.** Line-intersection is fast (one STRtree query + a handful of `intersects` tests per segment). The 100 m depth sampling on each segment dominates cost. Total compute typically under 5 s; not user-visible against the existing forecast load latency.
- **Venue ingest is per-venue.** Run `python -m workers.enc_ingest --region chicago` to refresh. ENC updates weekly, so cron schedule isn't critical — monthly is probably enough.
- **`SLCONS` is the breakwall layer.** If a future user reports a route cutting through a structure you'd swear is a breakwall, first check whether SLCONS for that venue actually contains it (open the GeoJSON in QGIS or grep for nearby coordinates).

## Follow-ups not addressed this session

These came up while debugging but aren't blocking the breakwall fix.

1. **Redis pub/sub `TimeoutError` on SSE notifications.** The `routing_notifications.py` better-route stream times out reading from Memorystore after ~5 min idle. Cosmetic — doesn't block compute. Add `health_check_interval` to the async Redis client or wrap `pubsub.listen()` to catch `TimeoutError` and resubscribe. ~10 lines.
2. **CONUS hazards file is still small** (~600 KB, mostly RESARE that gets filtered). The general-scale layer IDs were corrected in `GENERAL_HAZARD_LAYERS` but a fresh CONUS ingest hasn't been run with the new IDs yet. Worth doing before the next offshore-passage test (Mac, Newport Bermuda, etc.) where general-scale features matter.
3. **`CONUS` hazard count is logged as part of total but actual contribution is small.** Once we re-ingest CONUS we'll see numbers like "depth + 5,000 polygons across 2 indices" instead of the current "127 across 2."
4. **DYKCON / FSHFAC return 0 features in Chicago.** Plausible — Lake Michigan doesn't have many dykes or fish weirs — but worth verifying for a venue that should have them (e.g. Chesapeake fish weirs, Dutch-influenced harbours). Layer IDs may have drifted there too.
5. **RESARE filter is global and crude.** Comment in `charts/__init__.py` mentions parsing CATREA per feature to keep navigation-blocking subcategories. A v1.x improvement; flagging again because more venue ingests will surface more "too restrictive RESARE" complaints.
6. **No automated cron for venue ENC ingests.** Currently runs manually from a dev machine with ADC. Cloud Run Job triggered by Scheduler would be the natural shape; deferred until there's a second venue with regular updates needed.
7. **`route_recompute.py` venue change isn't covered by tests.** Existing tests for that worker mock at the engine layer; the venue resolution branch is currently exercised only by deployment.

## Pre-ship feature backlog

The breakwall fix unblocks the Mac (Chicago → Mackinac, Lake Michigan transit). Northerly Island avoidance similarly unblocks any venue with a near-shore mark. No specific feature is now blocked on this work.

Backlog from previous sessions remains:

- `infra/schema.sql` ownership transfer for `user_profiles` and `race_sessions`. Two-line append, but only matters for fresh-DB bootstrap.
- `pythonpath = .` in `backend/pytest.ini`. QoL fix.
- Long-distance course presets in `morfCourses.js`.

## Next session

Pick up either:

- **Redis SSE timeout fix** (small, satisfying, removes the noisy stack traces from logs).
- **CONUS hazard re-ingest** (one command, sets us up for offshore-passage racing).
- **Multi-region venue rollout** (Annapolis, Newport, SF Bay are the obvious next ones — same `python -m workers.enc_ingest --region <name>` per venue).
- **First real-race test on Lake Michigan** with the now-functional routing. The Wednesday-night beer-can fleets out of Chicago and Milwaukee start running in a couple weeks.
