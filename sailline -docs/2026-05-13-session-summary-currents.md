# 2026-05-13 — Stream 2 — Currents ingest (NOAA OFS)

Companion to the earlier 2026-05-13_session.md (Sprint 1/2 — engine v9
+ AIS). This session implements the **surface-currents** pillar from the
Stream 2 backlog: ingestion, native-grid storage, time-bracketing
sampler, and engine integration. Engine bumped v9 → **v10-currents**.

## What we worked on

Closed the first item on the Stream 2 ingest list: **currents (NOAA
OFS)**. The engine already accepted a `currents=` sampler from the v9
work; this session built the ingest path, the loader, and wired it
into both the synchronous routing endpoint and the background
recompute worker.

The user-facing change is small: routes computed in OFS-covered waters
now fold surface currents into the polar-projected position each
iteration. The `meta.currents_quality` field reports which source(s)
contributed — null when no OFS source covers the marks bbox.

Native-grid throughout: no regridding to a uniform lat/lon raster.
FVCOM sources keep their unstructured triangular mesh and use
barycentric interpolation; ROMS/POM sources keep their curvilinear
structured grid and use inverse-distance weighting from the 4 nearest
wet cells. Decision driven by need for shoreline fidelity around
complex embayments — regridding the Great Lakes FVCOM to a regular
grid would smooth out coves and harbour mouths where currents matter
most.

Sources covered (11 total):

| Source | Water body | Grid type |
|---|---|---|
| `lmhofs` | Lake Michigan + Huron + St. Clair | FVCOM |
| `lsofs` | Lake Superior | FVCOM |
| `leofs` | Lake Erie | FVCOM |
| `loofs` | Lake Ontario | FVCOM |
| `sfbofs` | San Francisco Bay | FVCOM |
| `cbofs` | Chesapeake Bay | ROMS |
| `dbofs` | Delaware Bay | ROMS |
| `tbofs` | Tampa Bay | ROMS |
| `gomofs` | Gulf of Maine | ROMS |
| `ngofs2` | Northern Gulf of Mexico | ROMS |
| `nyofs` | NY / NJ Harbor | POM |

Both forecast (6h-cycle, f000..f120) and nowcast (hourly, n001..n006)
ingest paths are implemented; deployed as separate Cloud Run Jobs per
(source, run_type) pair.

## Files changed

| Path | Change |
|---|---|
| `backend/app/currents_regions.py` | **NEW.** OFS source registry, separate from wind regions. `CurrentSource` dataclass with bbox / url_for / fhour_range(run_type). Lookups: `sources_covering_marks`, `sources_covering_point`, `by_grid_type`. |
| `backend/app/services/currents/__init__.py` | **NEW.** Package exports. |
| `backend/app/services/currents/netcdf_extract.py` | **NEW.** NetCDF → FvcomMesh + FvcomSnapshot / RomsGrid + RomsSnapshot. ROMS de-staggering + true-east/north rotation done at extract time. Lazy KDTree cache lives on the mesh/grid (shared across all snapshots referencing it). |
| `backend/app/services/currents/fields.py` | **NEW.** `FvcomCurrentField` (KDTree-of-centroids + barycentric), `RomsCurrentField` (IDW from 4 nearest wet cells), `CurrentForecast` (time-bracket wrapper mirroring `WindForecast`), `CurrentsUnavailable`. |
| `backend/app/services/currents/loader.py` | **NEW.** `load_currents_for_race` — async Redis loader. Merges nowcast + forecast manifests, dedups at tied valid_times (forecast wins), brackets the race window with one fhour on each side. |
| `backend/workers/currents_ingest.py` | **NEW.** Cloud Run Job. Whole-file NetCDF download from NOMADS (no `.idx` sidecar exists for NetCDF); native-grid extract; topology written once per source under `currents:{source}:topology`, per-fhour blobs under `currents:{source}:{cycle}:{f\|n}{fhour:03d}`. Cycle ZSET shared across run types, trimmed to last 4. |
| `backend/app/routers/routing.py` | Bumped `ENGINE_VERSION = "v10-currents"`. New `_load_currents_optional` helper swallows `CurrentsUnavailable` and any other exception → None. Cache key now ends in `currents={tag}` where tag captures source(s), t_min, t_max. `meta.currents_quality` exposed. |
| `backend/workers/route_recompute.py` | Mirrors the router's currents-load policy so background "better route" alerts are apples-to-apples with the synchronous endpoint. |
| `backend/requirements.txt` | Uncommented `netCDF4==1.7.2` (bumped from 1.7.1 to get cp313 wheel). `scipy==1.14.1` was already pinned (cKDTree). |
| `backend/tests/test_currents_regions.py` | **NEW.** Registry + bbox + URL + fhour_range + sources_covering_marks (Mac course). 18 tests. |
| `backend/tests/test_currents_fields.py` | **NEW.** Pure-function helpers + FvcomCurrentField + RomsCurrentField + CurrentForecast time interpolation + multi-source pickup + shared-KDTree assertion. 17 tests. |
| `backend/tests/test_currents_loader.py` | **NEW.** `_FakeRedis` pattern from `test_forecast_loader`. Happy path, nowcast+forecast merge, missing topology, no cycle, `_pick_bracketing` dedup. 9 tests. |
| `backend/tests/test_currents_ingest.py` | **NEW.** Serialise roundtrip, latest_cycle math, run-type alias normalisation, dry-run cycle ingest with stubbed `_fetch_one`. 11 tests. |
| `backend/tests/test_routing_router_currents.py` | **NEW.** `_load_currents_optional` swallow contract, `_currents_cache_tag` states, ENGINE_VERSION assertion. 9 tests. |

All 288 collected tests pass (`pytest -m "not slow"` — 4 deselected
slow tests as expected).

## Decisions and rationale

* **Separate currents registry (`app/currents_regions.py`), not an
  extension of `app/regions.py`.** OFS publishes per-water-body
  (LMHOFS = Lake Michigan + Huron only) and these don't 1:1 map to
  wind regions. The `conus` wind region covers all five Great Lakes +
  both coasts; the `chicago` venue is a slice of LMHOFS. Forcing
  currents config onto every wind region would distort the wind
  contract (Hawaii has no OFS coverage) and break the orthogonality
  between wind ingest and currents ingest. Race-time lookup is
  `sources_covering_marks(marks) → 0..N CurrentSource` — independent
  of wind region resolution.

* **Native grids, no regridding.** FVCOM unstructured meshes preserve
  the shoreline fidelity that Great Lakes routing actually depends on
  — embayments, harbour mouths, breakwall gaps. Regridding to a regular
  lat/lon raster at 0.01° would smooth these into nothing. The cost is
  a more complex sampler (KDTree + barycentric vs simple bilinear) but
  the per-sample latency stays in the µs range. ROMS/POM grids are
  preserved as their native curvilinear 2-D arrays for the same
  reason, even though shoreline complexity is less of a problem there.

* **Topology stored once per source, per-fhour blobs are u/v only.**
  Mesh / grid is static — identical across every cycle for a given
  source. Stored under `currents:{source}:topology` with a 30-day TTL,
  re-written by the ingest worker only when absent. Cuts the Redis
  per-cycle footprint roughly 10x compared to embedding the topology
  in every snapshot.

* **Shared KDTree per mesh, not per field.** First implementation
  built a fresh KDTree on each `FvcomCurrentField` — meaning ~24
  redundant builds (one per fhour) per route compute. Refactored to
  cache the index on `FvcomMesh` / `RomsGrid` via lazy properties. The
  loader hands the same mesh instance to every field for one source,
  so one build serves all 24 fhours. Per-route overhead dropped from
  ~1.2 s to ~50 ms (Lake Michigan scale).

* **Currents are optional, never block a route compute.** The router's
  `_load_currents_optional` swallows `CurrentsUnavailable` and any
  other exception → returns None. The engine accepts None as a no-op.
  Background recompute uses the same policy so its alerts match the
  endpoint. Currents are an accuracy enhancement, not a hard
  dependency — a Redis blip, a missing OFS cycle, or a race outside
  OFS coverage all degrade gracefully.

* **Forecast preferred over nowcast at tied valid_time.** Nowcast n001
  and forecast f000 both publish at the cycle reference time. Dedup
  preference: forecast wins. Cycle-stability rationale — f000 is what
  the next cycle's analyzed nowcast will most closely reproduce, so
  cache invalidation behaves more predictably.

* **One Cloud Run Job per (source, run_type), not a single
  multi-source job.** Per-source isolation: if NOAA serves a corrupt
  LMHOFS file, the LSOFS job keeps running. Trade-off is 22 jobs
  instead of one, but they're cheap and the Cloud Scheduler config is
  declarative. Forecast vs nowcast also get different schedules.

* **netCDF4 bumped to 1.7.2 from 1.7.1.** 1.7.1 only ships wheels up
  to cp312; on Python 3.13 (the user's dev environment) pip falls
  through to a source build that requires HDF5 headers and fails.
  1.7.2 has cp313 wheels.

## Open items / next steps

* **Run pytest on Windows after deploy.** Already done in this
  session — all 288 selected tests green.

* **Provision Cloud Run Jobs + Scheduler triggers** for the worker:

  ```powershell
  # 11 sources × 2 run types = 22 jobs. Pattern per job:
  gcloud run jobs create sailline-currents-lmhofs-forecast `
    --source backend `
    --command "python" --args "-m,workers.currents_ingest,lmhofs,--run-type,forecast" `
    --vpc-connector <existing> `
    --set-env-vars "GCS_CURRENTS_BUCKET=sailline-currents,REDIS_HOST=...,REDIS_PORT=6379"

  # Scheduler — forecast every 6h (~30 min after each NOAA cycle):
  gcloud scheduler jobs create http currents-lmhofs-forecast `
    --schedule="30 1,7,13,19 * * *" `
    --uri="..."

  # Scheduler — nowcast hourly:
  gcloud scheduler jobs create http currents-lmhofs-nowcast `
    --schedule="45 * * * *" `
    --uri="..."
  ```

* **Provision `GCS_CURRENTS_BUCKET`** as a separate bucket from
  `GCS_WEATHER_BUCKET`. Lifecycle policy can be more aggressive
  (NetCDF blobs are larger but less reused historically).

* **Stream 2 backlog remaining** (after this session lands):
  - **Waves** (GLERL + WaveWatch III). Engine already derates polar
    by `hs_m`; router currently defaults it to 0. Adding the ingest
    unlocks real wave-aware routing. Similar shape to currents but
    even simpler — wave data fits on a regular lat/lon grid.
  - **Regulatory zones** (extend `enc_ingest.py` to also load
    `RESARE` / `TSSLPT` / `PRCARE` feature codes). Small — the hazard
    pipeline already exists.
  - **GEFS ensemble** (probabilistic forecasts). Bigger lift; needs
    a percentile aggregator and a UI confidence-fan to be useful.
    Best paired with the eventual UI work.
  - **Wind shear** ingest. Engine integration blocked on polar-format
    upgrade (the CSV polars don't carry an effective-wind-height
    parameter). Ingest can land independently and park the engine
    wiring.

* **Stream 4 UI work** (after Stream 2 ingest fully lands): currents
  overlay on the map, currents_quality badge in the route panel,
  start-line geometry, rounding-side toggle, regulatory-zone polygons.

## Technical debt flagged

* **`_load_currents_optional` is duplicated** in `routers/routing.py`
  and `workers/route_recompute.py`. Identical bodies, identical
  policy. Lift into a shared helper module if it grows; for now the
  duplication keeps the router and worker independently deployable.

* **No real-NetCDF integration test.** Every extract-side test uses
  synthetic numpy arrays. A `RUN_NOAA_SMOKE=1`-gated test against
  live NOMADS would catch any format drift in OFS output. Deferred —
  the synthetic-data coverage on the transform helpers (sigma-layer
  selection, ROMS de-staggering, barycentric, IDW) is solid.

* **Cross-route KDTree cache.** Within one route compute, KDTrees
  are shared across all fhours of a source. Across compute calls,
  the topology is deserialised fresh and the index rebuilt — costs
  ~50 ms per source per compute. A module-level
  `LRU(source_name, cycle_iso) → mesh+index` cache would amortise
  this further. Deferred until measurement says it matters.

* **JSON serialisation for per-fhour blobs is fat for FVCOM.**
  ~300 KB gzipped per fhour × 24 fhours × 11 sources × 4 cycles
  retained = ~300 MB Redis hot. Fine for current Memorystore size.
  A binary format (msgpack with numpy support, or `np.savez_compressed`
  blobs in GCS with a Redis pointer) would cut this 3-5x. Revisit if
  Memorystore memory becomes a constraint.

* **ROMS sampler uses IDW, not full curvilinear bilinear.** Trade-off
  flagged in the docstring — IDW handles the wet/dry boundary
  gracefully (a NaN land cell is simply skipped) but loses a tiny
  amount of fidelity at sub-cell scales. Engine doesn't care at
  routing resolution. Replace with proper curvilinear bilinear if a
  ROMS-region race compute reveals routing artefacts.

## Manual follow-up

```powershell
cd E:\Personal\Coding\SailLine\backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt        # picks up netCDF4 1.7.2
pytest -m "not slow"                   # baseline — already green
```

Optional dry-run smoke check (writes to `backend/ingest_output/`,
needs internet but no Redis/GCS creds):

```powershell
python -m workers.currents_ingest lmhofs --run-type forecast --fhour 1 --dry-run
```
