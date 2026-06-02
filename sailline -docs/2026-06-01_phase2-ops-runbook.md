# 2026-06-01 — Phase 2 ops runbook

Run AFTER Phase 1 is fully deployed (`alembic current` reads `0018`,
Cloud Run revision running the Phase 1 code, `min-instances=1` set).

Phase 2 adds the `recorder_debriefs` table, a new POST endpoint, and a
mobile build that records a ring buffer + posts a debrief at stop().
This needs both a backend deploy (migration + code) AND a fresh mobile
EAS build before the on-water test.

PowerShell only. Backtick continuations.

---

## 0. Pre-flight

```powershell
gcloud builds list --limit=1 --ongoing
```

Empty = no deploy in flight. Good to start.

---

## 1. Run backend tests locally

From `backend/` with the venv active:

```powershell
cd E:\Personal\Coding\SailLine\backend
.\.venv\Scripts\Activate.ps1
pytest -m "not slow" -q
```

Expected: all tests pass including the new `test_recorder_debrief.py` suite
(8 tests covering auth, validation, persistence, insert-only).

If anything fails — STOP and tell me. Do not proceed.

---

## 2. Type-check the mobile changes

```powershell
cd E:\Personal\Coding\SailLine\mobile
npx tsc --noEmit
```

Expected: clean exit. No new TS errors.

If errors surface, fix forward — they're almost certainly typos in
the new files I just wrote.

---

## 3. Apply migration 0019

The Cloud SQL proxy still needs to be running from Phase 1, or restart it:

```powershell
cd E:\Personal\Coding\SailLine
.\cloud-sql-proxy.exe sailline:us-central1:sailline-db --port 5432
```

In a separate window:

```powershell
cd E:\Personal\Coding\SailLine\backend
.\.venv\Scripts\Activate.ps1

$env:DB_HOST="127.0.0.1"
$env:DB_PORT="5432"
$env:DB_NAME="sailline_app"
$env:DB_USER="sailline"
$env:DB_PASSWORD=(gcloud secrets versions access latest --secret=sailline-db-app-password).Trim()

alembic current        # expect: 0018 (head)
alembic upgrade head   # applies 0019
alembic current        # expect: 0019 (head)
```

If `alembic current` shows nothing, swap `DB_USER=postgres` and the secret name to `sailline-db-postgres-password`.

The migration creates the `recorder_debriefs` table + a single composite index. Purely additive — no risk of failing on existing data.

---

## 4. Commit + push backend

```powershell
cd E:\Personal\Coding\SailLine

git add backend/migrations/versions/0019_add_recorder_debriefs.py `
        backend/app/routers/recorder_debrief.py `
        backend/app/main.py `
        backend/tests/test_recorder_debrief.py `
        mobile/src/recorder/recorderLog.ts `
        mobile/src/recorder/debrief.ts `
        mobile/src/recorder/useTrackRecorder.ts `
        mobile/src/api/recorderDebrief.ts `
        mobile/src/screens/RecorderDebugScreen.tsx `
        mobile/src/components/RaceListSheet.tsx `
        "mobile/app/(app)/recorder-debug.tsx" `
        "sailline -docs/2026-06-01_phase2-ops-runbook.md"

git status   # sanity check before committing

git commit -m "phase2: recorder ring buffer + debrief endpoint + debug screen"

git push origin main
```

Cloud Build picks up the backend changes automatically. Watch:

```powershell
gcloud builds list --limit=1 --ongoing
```

Wait until empty (deploy done).

---

## 5. Smoke test the deployed endpoint

A quick curl against the live URL — substitute a real Firebase ID token
from your signed-in session (Firebase console or DevTools network tab):

```powershell
$RACE_ID = "<some race uuid you own>"
$TOKEN = "<your firebase id token>"

$payload = @{
    schema_version = 1
    device = @{ platform = "android"; os_version = "14"; app_version = "0.1.0"; build_id = $null }
    session = @{
        start_ts = "2026-06-01T22:00:00Z"
        end_ts   = "2026-06-01T22:05:00Z"
        duration_s = 300
    }
    capture = @{ points_captured = 300; points_uploaded = 300; points_remaining_in_queue = 0; max_queue_depth = 5 }
    uploads = @{ attempts = 10; successes = 10; http_5xx = 0; http_4xx = 0; network_errors = 0; longest_success_gap_s = 31.0 }
    recent_log = @()
} | ConvertTo-Json -Depth 6

Invoke-WebRequest `
  -Uri "https://sailline-api-105706282249.us-central1.run.app/api/races/$RACE_ID/recorder-debrief" `
  -Method POST `
  -Headers @{ "Authorization" = "Bearer $TOKEN"; "Content-Type" = "application/json" } `
  -Body $payload
```

Expected: 201 with a JSON body containing `id` and `created_at`.

Then verify in Cloud SQL Studio:

```sql
SELECT id, session_id, created_at, payload->'capture'->>'points_captured' AS captured
FROM recorder_debriefs
ORDER BY created_at DESC
LIMIT 3;
```

You should see your row.

---

## 6. Build a new mobile preview APK

The mobile changes won't take effect on your phone until you install a
fresh build. From `mobile/`:

```powershell
cd E:\Personal\Coding\SailLine\mobile
eas build --profile development --platform android
```

This kicks off the EAS build (~15-25 min). When it finishes, EAS emails
you a link to install the APK on your phone.

---

## 7. Verify on device

After installing the new APK:

1. Open the app, sign in, pick or create a race.
2. Start recording. Let it run for ~2 minutes.
3. Stop recording.
4. Open Cloud SQL Studio and check:

   ```sql
   SELECT id, created_at,
          payload->'capture'->>'points_captured' AS captured,
          payload->'capture'->>'points_uploaded' AS uploaded,
          payload->'uploads'->>'longest_success_gap_s' AS gap_s
   FROM recorder_debriefs
   WHERE session_id = '<your race uuid>'
   ORDER BY created_at DESC;
   ```

   You should see one row with sensible counts.

5. On your phone, **long-press the email address** at the top of the
   home screen for ~1.5 seconds. The Recorder Debug screen opens. You
   should see the recent log entries from that session.

If the debrief row is missing OR the debug screen is empty, the
mobile pipeline isn't working — tell me, don't proceed to on-water.

---

## 8. On-water validation (the real test)

Sail a short race (15-30 min is fine). Don't change anything from your
usual flow. After stopping the recording:

1. Open the debug screen (long-press the email). Note the queue
   length, last fix time, and total log entries.
2. Open Cloud SQL Studio and check the debrief row's
   `longest_success_gap_s`. **This is the headline number** — it tells
   us whether the upload pipeline went silent.

   - **< 60 s** — Phase 1 + 2 are doing their job. The upload-pipeline
     issue is materially improved (or the conditions were forgiving).
     Move to Phase 3 (status badge).
   - **> 5 min anywhere** — the underlying problem is still present
     (expected, until Phase 4's native uploader ships). The debrief
     gives us the data we need to design Phase 4 with confidence.

Either result is a successful test of Phase 2's diagnostic value.

---

## 9. Done — Phase 2 complete

Phase 3 (UI upload-status badge driven by the same stats) is next, then
Phase 4 (Transistorsoft native uploader — the actual upload fix).
