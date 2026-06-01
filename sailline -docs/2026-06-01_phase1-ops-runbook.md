# 2026-06-01 — Phase 1 ops runbook

Run these in order. Each step is gated on the previous step succeeding.
PowerShell only. Backtick line continuations, not backslash.

---

## 0. Pre-flight: confirm what's running

Confirm there's no in-flight Cloud Build before you push anything.

```powershell
gcloud builds list --limit=1 --ongoing
```

Empty result is fine — means no deploy is mid-flight.

---

## 1. Check for duplicate (session_id, recorded_at) rows BEFORE the migration runs

The migration 0018 adds `UNIQUE (session_id, recorded_at)` on `track_points` and `imu_samples`. If any duplicate already exists, `ALTER TABLE … ADD CONSTRAINT` fails and the migration aborts. Find out first.

Open a psql session against the prod Cloud SQL DB (or use the Cloud SQL proxy if that's how you usually connect):

```powershell
# Adjust connection details to match your usual psql invocation.
# This expects the proxy on 127.0.0.1:5432 or your usual creds.
psql -h 127.0.0.1 -U sailline -d sailline_app
```

Once in:

```sql
-- Count duplicates in track_points.
SELECT COUNT(*) AS dup_pair_count
FROM (
    SELECT session_id, recorded_at
    FROM track_points
    GROUP BY session_id, recorded_at
    HAVING COUNT(*) > 1
) d;

-- Count duplicates in imu_samples.
SELECT COUNT(*) AS dup_pair_count
FROM (
    SELECT session_id, recorded_at
    FROM imu_samples
    GROUP BY session_id, recorded_at
    HAVING COUNT(*) > 1
) d;
```

**Three outcomes:**

- **Both return 0** → no cleanup needed. Skip to step 2.
- **Either returns a small number (< 1000 rows)** → run the cleanup block in step 1a. Quick and safe.
- **Either returns a large number (> 1000 rows)** → STOP and report back. We'll talk through the cleanup before running it, and may schedule it to run off-hours if it's bigger than expected.

### 1a. Cleanup duplicates (only if step 1 found any)

Drops all but the earliest-inserted copy of each (session_id, recorded_at) pair. Wrapped in BEGIN/COMMIT so a mistake rolls back cleanly. **Run inside the same psql session.**

```sql
BEGIN;

-- track_points: keep the row with the smallest ctid (== earliest-inserted)
-- per (session_id, recorded_at) pair.
DELETE FROM track_points a
USING track_points b
WHERE a.session_id = b.session_id
  AND a.recorded_at = b.recorded_at
  AND a.ctid > b.ctid;

-- imu_samples: same pattern.
DELETE FROM imu_samples a
USING imu_samples b
WHERE a.session_id = b.session_id
  AND a.recorded_at = b.recorded_at
  AND a.ctid > b.ctid;

-- Re-check — both should now return 0.
SELECT 'track_points' AS table, COUNT(*) AS dup_pair_count
FROM (
    SELECT session_id, recorded_at
    FROM track_points
    GROUP BY session_id, recorded_at
    HAVING COUNT(*) > 1
) d
UNION ALL
SELECT 'imu_samples',
    COUNT(*)
FROM (
    SELECT session_id, recorded_at
    FROM imu_samples
    GROUP BY session_id, recorded_at
    HAVING COUNT(*) > 1
) d;

-- If both rows show 0, commit. Otherwise ROLLBACK and stop.
COMMIT;
```

---

## 2. Run backend tests locally

From `backend/` in a venv with the project deps installed:

```powershell
cd E:\Personal\Coding\SailLine\backend
pytest -m "not slow" -q
```

Expected: all tests pass, including the new ones —
`test_telemetry_load_uses_for_update`,
`test_post_telemetry_reports_actual_landed_count`,
`test_post_telemetry_reports_zero_when_full_duplicate_batch`,
`test_post_telemetry_imu_insert_uses_on_conflict`,
`test_post_reports_actual_landed_count`,
`test_post_reports_zero_when_full_duplicate_batch`,
`test_post_load_uses_for_update`,
`test_post_wraps_work_in_transaction`,
`test_load_race_for_ingest_acquires_for_update`.

If any fail, do NOT proceed. Report the failure and we fix forward.

---

## 3. Apply the Alembic migration

From `backend/` with the venv active and database creds set:

```powershell
cd E:\Personal\Coding\SailLine\backend
alembic current        # should print 0017
alembic upgrade head   # applies 0018
alembic current        # should now print 0018
```

If `alembic upgrade head` errors with `duplicate key value violates unique constraint`, step 1's dedup didn't catch everything. Roll back with `alembic downgrade -1` (re-runs without the failed constraint), re-check, retry.

Per the migrations runbook: this is an additive migration, no destructive column changes. Safe to apply ahead of the matching code push.

---

## 4. Commit + push the code

The migration must already be applied in step 3 BEFORE the code with `ON CONFLICT (session_id, recorded_at)` ships, otherwise the SQL fails at runtime. Order matters.

```powershell
cd E:\Personal\Coding\SailLine
git add backend/migrations/versions/0018_track_points_idempotency.py `
        backend/app/routers/telemetry.py `
        backend/app/routers/tracks.py `
        backend/app/services/track_ingest.py `
        backend/tests/test_telemetry.py `
        backend/tests/test_tracks_router.py `
        backend/tests/test_track_ingest.py `
        "sailline -docs/2026-06-01_replan-must-have-scope.md" `
        "sailline -docs/2026-06-01_durable-upload-pipeline-plan.md" `
        "sailline -docs/2026-06-01_phase1-ops-runbook.md"

git commit -m "phase1: idempotent telemetry ingest + FOR UPDATE row lock"

git push origin main
```

Cloud Build will pick it up and auto-deploy.

---

## 5. Set min-instances=1 on Cloud Run

This kills the cold-start hazard responsible for the 2026-05-31 15:33:09 500.

```powershell
gcloud run services update sailline-api `
  --region=us-central1 `
  --min-instances=1
```

Cost note: approximately $5/month of always-warm idle CPU, accepted per the 2026-06-01 plan. Verify with:

```powershell
gcloud run services describe sailline-api `
  --region=us-central1 `
  --format="value(spec.template.metadata.annotations.'autoscaling.knative.dev/minScale')"
```

Should print `1`.

---

## 6. Smoke test the deployed endpoint

Once `gcloud builds list --limit=1 --ongoing` is empty again:

```powershell
# Health.
curl https://sailline-api-105706282249.us-central1.run.app/health

# Should be {"status":"ok", ...}.
```

Then the same-batch-twice idempotency check via the mobile app: start a recording, capture ~30 s of GPS, stop. Re-open the app and verify the post-race row in DB has the expected `gps_inserted` count (not double). I can write a curl-based version of this if you'd rather not test through the mobile app.

---

## 7. Done — Phase 1 complete

Mark the plan doc's Phase 1 checklist done. Phase 2 (recorder telemetry / debrief endpoint) is next; it's independent of mobile-side changes so we can ship it in the next session if you want to move fast, or hold for the native uploader in Phase 4.
