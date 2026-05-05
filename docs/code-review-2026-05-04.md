# SailLine — Code Review & Status (2026-05-04)

## Where development stands

You're at **week 6 of a 10-week build** targeting v1 by Chicago–Mac (July 2026). The foundation is strong: the two hardest infra risks (cfgrib+eccodes in Cloud Run, VPC reach to Cloud SQL) are retired, and the app is live at `sailline.web.app` end-to-end.

**Shipped (as of latest commit `ea089c0`):**
- Auth (Firebase + tier-aware profile UPSERT), DB + Alembic (3 migrations applied), Redis, GCS, CI/CD auto-deploy
- Race CRUD with user-scoped queries, MORF mark library + 64 course presets, deg-min/decimal coord entry
- HRRR + GFS ingest workers; weather endpoint with ETag + Redis→GCS fallback
- Multi-region wind barbs (CONUS base + venue overlays) with adaptive density (bilerp zoomed-in, decimate zoomed-out)
- Race start time + live countdown; active-race persistence with 6h grace window
- Bundle splitting (lazy-loaded AppView, RaceEditor, RacesListView)

**Not yet started (the differentiators):**
- GPS track recording (`track_points` table exists, **zero consumers** — no router, no UI)
- Isochrone routing engine (the hardest piece, week 3–4 in original plan, slipped)
- Polars, AI advisor, AIS, Stripe gating, GLCFS currents

## Code review findings

### Strengths
- Clean lifespan-managed startup; non-fatal pool init keeps `/health` responsive even when DB/Redis are unreachable
- Firebase JWT verification correctly offloaded to a thread (no event-loop blocking)
- User-scoped queries on every races endpoint — no cross-user leaks
- ETag + `Cache-Control: max-age=300` on weather collapses fleet requests to one origin hit
- Excellent inline rationale (race.py docstring on JSONB handling, db.py on lazy connect, etc.)

### Issues — fix before launch

| Pri | Issue | Where |
|---|---|---|
| **P0** | No tests on `auth.py` — security boundary with zero coverage | `backend/app/auth.py` |
| **P0** | `/users/me` still leaks raw Firebase claims dict (TODO in code) | `backend/app/routers/users.py:24` |
| **P0** | `infra/schema.sql` missing `ALTER TABLE ... OWNER TO sailline` per 2026-05-01 open item — fresh DB bootstrap will repeat the perm hiccup | `infra/schema.sql` |
| **P1** | No CI tests gating deploy — Cloud Build builds + ships on green checkout, no `pytest` or `npm test` | `infra/cloudbuild.yaml` |
| **P1** | `weather_ingest.py` uses `os.environ[...]` directly — KeyErrors instead of clean config errors | `backend/workers/weather_ingest.py:_write_redis/_write_gcs` |
| **P1** | No structured logging / request IDs — Cloud Run logs are hard to correlate | backend-wide |
| **P1** | No rate limiting on public `/health`, `/`, or `/api/weather` | `backend/app/main.py` |
| **P2** | No frontend tests (no Vitest/Jest, no eslint/prettier configs) | `frontend/` |
| **P2** | `_SELECT_COLS` interpolated as f-string (constant, low risk, but worth a comment) | `backend/app/routers/races.py:79` |

---

## Sprint Plan: Week of May 4–10, 2026

### Sprint Goal
**Unblock the routing engine.** Get GPS track recording shipped end-to-end (table → API → UI) and stand up the isochrone routing prototype as a standalone script. By Friday, you should be able to record a track during a club race AND have a working isochrone calc against HRRR for a saved course.

### Capacity
~30 hrs (solo, 5 weekday evenings + one weekend block). Assume 1 evening lost to ops drift.

### Committed (P0)

**1. GPS Track Recording — end to end (8h)**
- `backend/app/routers/tracks.py` — `POST /api/races/{id}/track` (batched point insert), `GET /api/races/{id}/track` (full session playback)
- Pydantic models for batched ingest (50–200 points/payload)
- New `useTrackRecorder` hook in `frontend/src/hooks/` — wraps `navigator.geolocation.watchPosition`, batches at 5s cadence, posts when network available, queues otherwise
- "Record track" toggle in the active-race overlay on MapView
- Tests: `test_tracks_router.py` (insert + scope-by-user)
- Acceptance: record a 10-min walk around the block, refresh, see the breadcrumb on the map

**2. Isochrone Prototype (standalone) (10h)**
- `backend/scripts/isochrone_proto.py` — pure-numpy script, takes (start_lat, start_lon, finish_lat, finish_lon, polar_csv, wind_grid.json) → optimal route as GeoJSON
- Hardcoded Beneteau 36.7 polar from the PDF in `docs/First_36.7.pdf`
- Time-step isochrone fan; no obstacles for v0
- **Output to a notebook/HTML map for sanity check** — don't wire to Cloud Run yet
- Acceptance: produces a sailable route on a known wind field that visibly tacks upwind

**3. Backend test + auth coverage (3h)**
- `tests/test_auth.py` — mock `fb_auth.verify_id_token`, verify 401 on bad token, profile UPSERT idempotent, `require_pro` gate
- Strip `claims` from `/users/me` response

**4. Schema bootstrap fix (1h)**
- Add `ALTER TABLE user_profiles, race_sessions OWNER TO sailline;` to `infra/schema.sql`
- Document why in a comment block

### Stretch (P1)

**5. CI gate (2h)**
- Add `pytest` step to `infra/cloudbuild.yaml` before the `gcloud run deploy` step. Fail-fast.

**6. Long-distance course presets (2h)**
- Add Zimmer, Skipper's Club, Hammond entries to `frontend/src/lib/morfCourses.js`. Mark library already supports them.

**7. Structured logging (2h)**
- Switch `logging.basicConfig` to JSON formatter; add a request-ID middleware to FastAPI.

### Out of scope (next sprint)
- AI advisor wiring (Claude API) — needs routing output as input
- AIS / Datalastic — pure dependency on Pro tier gating, which depends on Stripe
- Polars beyond the hardcoded 36.7 — comes after isochrone is proven
- GLCFS currents

### Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Isochrone math eats the whole sprint | High | Time-box to 10h. If not sailable by Wed, ship simplified version (great-circle + VMG sanity) and defer isochrone to next sprint |
| `watchPosition` accuracy on a moving boat | Medium | Test on land first; defer offshore validation to a real club race |
| Polar PDF parsing rabbit hole | Medium | Hand-transcribe to CSV — 36.7 polar is ~30 cells, faster than scripting |

### Definition of Done
- All P0 items merged to `main`, deployed, smoke-tested in prod
- `pytest` clean; coverage on `auth.py` ≥ 80%
- Isochrone prototype produces a valid GeoJSON route saved in `backend/scripts/output/`
- A new `docs/2026-05-08-session-summary.md` captures decisions + open items
