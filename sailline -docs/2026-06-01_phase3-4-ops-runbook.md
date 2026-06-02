# 2026-06-01 — Phase 3 + 4 ops runbook

Phase 3 ships the visible upload-status badge on the recording screen.
Phase 4 ships the Transistorsoft native-uploader behind a feature flag,
OFF by default — you flip it on from the debug screen.

PowerShell only. Backtick continuations.

---

## 0. Prereq sanity

Phase 1 and Phase 2 must be deployed and validated:

```powershell
gcloud builds list --limit=1
# Expect: most recent build SUCCESS, not WORKING.

alembic current
# Should print 0019 (head). If 0018, Phase 2 hasn't shipped yet.
```

Cloud Run `sailline-api` should already have `min-instances=1` from Phase 1.

---

## 1. Install the new mobile dep

`@react-native-community/netinfo` is the only new RN dep. Expo manages
the install + native linking:

```powershell
cd E:\Personal\Coding\SailLine\mobile
npx expo install @react-native-community/netinfo
```

Verify it landed in `package.json` (already added by hand in the commit).

---

## 2. Run backend tests locally

There's one new validator (negative-sentinel coercion) on the GPS
sample model, plus two new tests:

```powershell
cd E:\Personal\Coding\SailLine\backend
.\.venv\Scripts\Activate.ps1
pytest -m "not slow" -q
```

Expected: all pass including `test_post_telemetry_coerces_negative_sentinels`
and `test_post_telemetry_accepts_valid_speeds_unchanged`.

---

## 3. Type-check the mobile changes

```powershell
cd E:\Personal\Coding\SailLine\mobile
npx tsc --noEmit
```

Expected: clean.

---

## 4. No migration this round

Phase 3 is mobile-only. Phase 4's backend touch is the sentinel
coercion in `app/routers/telemetry.py` — runtime behavior only, no
schema change. `alembic current` stays at `0019`.

---

## 5. Commit + push

```powershell
cd E:\Personal\Coding\SailLine

git add backend/app/routers/telemetry.py `
        backend/tests/test_telemetry.py `
        mobile/package.json `
        mobile/src/api.ts `
        mobile/src/recorder/uploadStatus.ts `
        mobile/src/recorder/featureFlags.ts `
        mobile/src/recorder/backgroundGeolocation.ts `
        mobile/src/recorder/tokenRefresh.ts `
        mobile/src/recorder/useTrackRecorder.ts `
        mobile/src/recorder/RecorderContext.tsx `
        mobile/src/components/UploadStatusBadge.tsx `
        mobile/src/screens/RecorderDebugScreen.tsx `
        "mobile/app/(app)/recording.tsx" `
        "sailline -docs/2026-06-01_phase3-4-ops-runbook.md"

git status

git commit -m "phase3+4: upload-status badge + native uploader (flag-gated)"

git push origin main
```

Watch the backend deploy:

```powershell
gcloud builds list --limit=1 --ongoing
```

When empty, the sentinel-coercion change is live.

---

## 6. Build a new EAS mobile dev build

The new netinfo native module + the new Transistorsoft http config
both require a fresh build (Expo Go can't ship custom native modules).

```powershell
cd E:\Personal\Coding\SailLine\mobile
eas build --profile development --platform android
```

Wait for the email; install the APK on your phone.

---

## 7. Validate Phase 3 (badge) — flag OFF baseline

This proves Phase 3 works without disturbing the existing JS uploader.

1. Open the app. Sign in. Long-press your email at the top of the
   home screen for ~1.5 seconds — the Recorder Debug screen opens.
2. **Verify the "Native uploader (Phase 4)" switch is OFF** (the
   default). Leave it off.
3. Go back to the home screen, pick a race, tap Start.
4. **Expect, top-left next to the red LIVE pill: a small green LIVE
   chip with a dot.** That's the new upload-status badge.
5. Capture for 30 s; verify the badge stays green.
6. Toggle your phone into airplane mode. Within ~5 seconds the badge
   should transition through BUFFER (with a count) to STALL or
   OFFLINE depending on which threshold trips first.
7. Toggle airplane mode off; within ~30 s the badge should return to
   LIVE as the backlog drains.
8. Stop recording.

If the badge never appeared or never changed, Phase 3 wiring is wrong
— tell me, don't continue to Phase 4 validation.

---

## 8. Validate Phase 4 (native uploader) — flag ON

This is the high-risk validation. We want proof that:

a. Transistorsoft's HTTP layer hits our endpoint with a body our
   backend accepts (no silent 422s).
b. The native onHttp event drives LiveStats so the badge stays accurate.
c. With the screen locked, uploads keep flowing (the core Phase 4 win).

Procedure:

1. Open the Recorder Debug screen (long-press email).
2. **Flip the "Native uploader (Phase 4)" switch to ON.** A note
   below it reminds you the flag takes effect on next Start.
3. Go back home, pick a race, tap Start.
4. Watch the recording screen for 60 s. The badge should reach LIVE
   within ~10 s (faster than JS mode, since autoSyncThreshold=1).
5. Check Cloud SQL Studio:

   ```sql
   SELECT COUNT(*) AS landed
   FROM track_points
   WHERE session_id = '<your race uuid>';
   ```

   Expect a non-zero, growing number. If it stays at 0 for > 60 s,
   the locationTemplate body shape is wrong and POSTs are 422'ing
   silently — flip the flag back OFF, stop the recording, look at
   the Cloud Logging for `sailline-api` to see the actual 422 body.

6. **Lock your phone for 5 minutes.** Native upload continues.
7. Unlock and check the Cloud SQL count again — should reflect
   ~5 minutes of fixes captured during the lock.
8. Stop recording.
9. Check the debrief:

   ```sql
   SELECT created_at,
          payload->'capture'->>'points_captured' AS captured,
          payload->'capture'->>'points_uploaded' AS uploaded,
          payload->'uploads'->>'longest_success_gap_s' AS gap_s,
          payload->'uploads'->>'attempts' AS attempts,
          payload->'uploads'->>'successes' AS successes,
          payload->'uploads'->>'http_5xx' AS h5xx,
          payload->'uploads'->>'http_4xx' AS h4xx,
          payload->'uploads'->>'network_errors' AS neterr
   FROM recorder_debriefs
   WHERE session_id = '<your race uuid>'
   ORDER BY created_at DESC
   LIMIT 1;
   ```

   The headline numbers:

   - `points_uploaded` should be within 1-2% of `points_captured`.
   - `longest_success_gap_s` should be under ~30 s.
   - `http_5xx` / `http_4xx` should be 0.

10. Open the debug screen and confirm the ring buffer has entries
    timestamped throughout the locked window. JS-mode would have had a
    flat-line during the lock — native should have entries throughout.

If all green: Phase 4 is working. **Stop here for the day**, do one
full on-water race tomorrow with the flag ON.

If anything red: flip the flag OFF on the debug screen, stop the
recording, and report which signal failed. We'll iterate on the
template / wiring / token refresh before retrying.

---

## 9. After two clean on-water races

We promote the flag default to ON in a follow-up commit (Phase 5
prep), then after a third clean race we delete the JS code path and
the flag entirely.

---

## 10. Done

Phase 3 + 4 are landed. The next phase (Phase 5 — cleanup) waits on
three clean on-water races with the native uploader on.
