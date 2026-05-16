# 2026-05-15 — Session E: `/telemetry` endpoint viability fix

## What we worked on

Made `POST /api/races/{race_id}/telemetry` actually work and brought
it to behavioural parity with the legacy `/track` endpoint. Three
problems found while reading the code that would have blown up the
moment the frontend (Session F) tried to switch over:

1. **Latent dead-on-arrival bug:** the INSERT named a column
   `location` — the actual column on `track_points` (migration 0002)
   is `position`. Every existing test was fully mocked, so the bug
   survived to prod-deployable state.
2. **Pre-D3 auth predicate:** `_verify_race_ownership` was a raw
   `user_id = $2` check. After D3 every other race-scoped endpoint
   moved to `race_write_predicate` (boat-crew aware). A frontend
   switch to `/telemetry` would have silently broken track-recording
   for crew on shared boats.
3. **No mark-rounding side effects:** `/track` detects roundings,
   persists them on `race_sessions.mark_passes`, and triggers the
   `race-postprocess` Cloud Run Job at the final mark. `/telemetry`
   did none of this. Auto-stop and post-race-stats triggering would
   have broken on cutover.

Fixed all three. Extracted the shared side-effect logic into a new
service module so both routers stay in lock-step from here on.

End state: 48/48 tests green for `test_track_ingest.py`,
`test_tracks_router.py`, `test_telemetry.py` against Python 3.10 in
the sandbox.

## Files changed

### Added

- **`backend/app/services/track_ingest.py`** — three small functions
  that both routers call:
  - `load_race_for_ingest(conn, race_id, uid)` — auth via
    `race_write_predicate`, returns `{"marks": [...],
    "mark_passes": [...]}`. 404 on non-writeable race.
  - `detect_and_persist_new_passes(conn, *, race_id, marks,
    existing_passes, new_points)` — runs the rounding detector
    resumed at the right index, UPDATEs `mark_passes` when at least
    one new pass landed, returns `(all_passes, new_passes)`.
  - `maybe_trigger_postprocess(race_id, marks, all_passes,
    new_passes)` — fires the `race-postprocess` Cloud Run Job iff
    THIS batch crossed the final mark. Returns `bool` (useful in
    tests). Never raises.

- **`backend/tests/test_track_ingest.py`** — 14 pure-function unit
  tests for the helper: JSONB-shape tolerance, 404 path,
  predicate-shape regression, detect/persist happy path,
  resume-from-existing, malformed marks, all-rounded short-circuit,
  trigger conditions.

### Modified

- **`backend/app/routers/tracks.py`** — refactored to call the shared
  helper. Behaviour-preserving. Removed local `_load_race_for_ingest`
  and `_detect_new_passes` (now in the helper). 343 → 242 lines.

- **`backend/app/routers/telemetry.py`** —
  - `location` → `position` in the GPS INSERT.
  - Switched GPS INSERT from `executemany` to `unnest`-based bulk
    insert (matches `/track`; single round trip per flush).
  - `_verify_race_ownership` removed; calls `load_race_for_ingest`.
  - Mark-rounding wired in: builds `DetectorPoint`s from the GPS
    portion of the batch, calls `detect_and_persist_new_passes`
    inside the transaction, `maybe_trigger_postprocess` after.
  - `TelemetryAck` gained `mark_passes` + `new_mark_passes` fields
    so the frontend's auto-stop hook keys off the same data shape
    whether it's posting to `/track` or `/telemetry`.

- **`backend/tests/test_tracks_router.py`** — monkeypatch target
  changed from `tracks.trigger_race_postprocess` to
  `track_ingest.trigger_race_postprocess` (the router no longer
  imports the trigger directly). Added one regression test
  (`test_post_uses_race_write_predicate`) asserting the auth SQL
  contains `boat_crew` and the `role IN` clause.

- **`backend/tests/test_telemetry.py`** — substantial rewrite:
  - Default `fake_conn.fetchrow` returns
    `{"marks": [far_mark], "mark_passes": []}` so existing tests
    don't accidentally trigger rounding.
  - GPS call-count asserts updated for the `unnest` switch:
    `execute` not `executemany` for GPS.
  - Ack-dict asserts updated for the new `mark_passes` +
    `new_mark_passes` fields.
  - New tests: `inserts_into_position_column` (regression for the
    column-name bug), `uses_race_write_predicate` (regression for
    the auth fix), `emits_mark_pass`, `resumes_from_existing_passes`,
    `triggers_postprocess_at_final_mark`,
    `does_not_trigger_intermediate_mark`,
    `does_not_trigger_when_no_new_passes`.
  - 293 → 551 lines, 16 → 18 tests.

## Decisions

| Decision | Rationale |
|---|---|
| Extract `load_race_for_ingest` + `detect_and_persist_new_passes` + `maybe_trigger_postprocess` into a service module rather than duplicating per-router. | Two endpoints with identical side effects WILL drift. The route_recompute worker comment in CLAUDE.md already calls out the same risk for venue resolution. Once `/track` is deprecated this module can absorb the GPS INSERT too. |
| Keep `executemany` for IMU (not `unnest`). | IMU schema differs from GPS, bounded at 1000 rows per batch. The performance delta is small and the diff isn't worth it for a stream we haven't started capturing yet. Future optimisation. |
| Frontend translates wire shape on flush (Option A from the next-session plan), backend keeps `t`/`sog_kts`/`cog_deg` field names. | Cleaner schema. Field aliases on `GpsSample` would confuse OpenAPI consumers. Recorder already has a local shape distinct from the wire shape. |
| `trigger_race_postprocess` is called outside the transaction. | A job failure must not roll back pass persistence. The trigger itself is fire-and-forget with built-in error tolerance. |
| `maybe_trigger_postprocess` returns `bool` for test introspection rather than emitting an event. | The existing pattern (monkeypatch the underlying trigger) was good enough. No new event abstraction needed. |
| Predicate-shape regression tests (`uses_race_write_predicate`) instead of a typed wrapper. | Same approach as `test_auth_predicates.py` — assert on the SQL fragment we emit. Cheap and direct. |

## Verification

```powershell
cd E:\Personal\Coding\SailLine\backend
pytest tests/test_track_ingest.py tests/test_tracks_router.py tests/test_telemetry.py -v
```

Expected: 48 passed.

Sandbox run on Python 3.10 with project-pinned `fastapi==0.115.0`:
all 48 green. Two pre-existing failures in `test_races_router.py`
(`datetime.fromisoformat` rejecting the `Z` suffix) are Python 3.10
vs 3.11+ standard-library differences, NOT caused by this session —
they pass on Windows CI.

## Open items / things to do when back

1. **Manual verification on Windows:**
   - `cd E:\Personal\Coding\SailLine\backend; pytest -m "not slow"`.
     Should be green except possibly the pre-existing
     `test_races_router.py` row-ownership tests if you ran on
     Python 3.10. (Your dev machine is 3.11+, no issue.)
   - Review the diff. `git diff --stat HEAD` should show 4 modified
     + 2 new backend files + 2 untracked docs.
2. **`.git\index.lock` cleanup** — leftover from the bash-mount
   memory issue earlier in the session. Run
   `Remove-Item .git\index.lock` if it's still there.
3. **Review and commit.** Suggested message:
   `fix(telemetry): use position column, race_write_predicate auth, mark-rounding parity with /track`
   The 6 frontend lines (`AppView.jsx`, `RaceEditor.jsx`) that show
   up as modified are pre-existing user-side edits, NOT this session.
   Stage and commit Session E changes separately.
4. **Deploy.** Cloud Build runs `pytest -m "not slow"` as the gate;
   push to `main` and watch. No migration in this session (additive
   only to columns that already exist).
5. **Session F is unblocked** — the frontend recorder can now switch
   to `/telemetry` with no other backend work required. Plan doc:
   `sailline -docs/2026-05-15_next-session-plan.md` §"Session F".
6. **Independent of Session E: Session A.fin.** The Capacitor Android
   setup is still waiting; runbook at
   `sailline -docs/2026-05-14-android-setup-runbook.md`.
7. **Update `Development plan.docx`** — I deliberately did NOT touch
   the .docx (no python-docx round trip without your eyes on it).
   The "Suggested next-session ordering" item #1 ("Adapt frontend GPS
   capture to `/telemetry`") is now genuinely unblocked.

## Tech debt flagged

| Item | Why it's debt | When to address |
|---|---|---|
| `/track` is now functionally redundant with `/telemetry` — only difference is the wire-payload shape. | Two paths into the same DB invite drift the moment one gets a new feature. | After Session F has shipped and one real race recorded successfully against `/telemetry`. Then delete `/track`, its router, and its tests. |
| No real-DB test runs the telemetry INSERT against a Postgres schema. | This is exactly why the `location`/`position` bug survived. The mocked tests can't catch a column-rename. | Add a smoke-tier test (env-gated like the NOAA tests) that hits a throwaway DB with the actual schema. Low priority — the new `inserts_into_position_column` test catches *this* class of bug at SQL-string level. |
| Phrf cert file (`phrf_cert.py`) still has a trailing-whitespace blob appended after the last function (visible in `git diff` from before the session). Not introduced by this session — pre-existing. | Cosmetic; pre-commit hook would catch it. | Squash in the next routine commit. |
| Migration 0015 + the avatars GCS bucket from D4 are still in "open deployment items" status on the docs. `infra/cloudbuild.yaml` already has `GCS_AVATARS_BUCKET=sailline-avatars`. | Verify the bucket exists and `alembic upgrade head` has been run against prod before next deploy or `/me/avatar` 503s on first user touch. | Run `gcloud storage buckets describe gs://sailline-avatars` and `alembic current` from a Cloud SQL Auth Proxy session. |
| Sandbox-side build of pytest deps takes ~30s and pinned `fastapi==0.115.0` is needed for the 403/401 contract on `HTTPBearer`. | Not a project issue — purely a note that my run env here has to match the project pins. | None — the project requirements.txt is authoritative. |

## Things I deliberately did NOT do

- Did NOT touch `frontend/`. Session F is the migration; running
  ahead of plan-review would bypass the "Plan before paste" rule.
- Did NOT delete the `/track` endpoint or any of its tests. Will be
  removed in the post-Session-F cleanup once a real race has
  recorded successfully through `/telemetry`.
- Did NOT run a migration. Sessions E touches no schema —
  `track_points` already had `position` and `gps_acc_m`.
- Did NOT touch `Development plan.docx`. Notes for it are in the
  "Open items" section above.
- Did NOT install or run vitest in the sandbox (memory note —
  bus errors regardless).
