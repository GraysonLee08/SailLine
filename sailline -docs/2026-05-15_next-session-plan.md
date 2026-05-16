# Next-Session Plan — drafted 2026-05-15

**Author:** prep work during dinner break, not yet reviewed.
**Goal:** Clear "what's next" so the next session is a single context window of execution, not exploration.

---

## TL;DR

Three sessions are queued. Do them in this order:

1. **Session A.fin** — finish the Capacitor Android setup runbook on Windows. Pure user-side work (no code I can write); the JS adapter and runbook already exist. Acceptance = 15-min screen-locked smoke test passes.
2. **Session E — `/telemetry` viability fix.** The endpoint has a latent dead-on-arrival bug (`column "location" does not exist`) and is out of sync with the D3 sharing/auth model. Must be fixed before any frontend migration. Backend-only, ~½ session.
3. **Session F — frontend GPS capture migration to `/telemetry`.** Swap the recorder's flush target. Backwards-compatible with existing races. ~1 session.

Sessions A.fin and E are independent — A.fin runs on the Windows side, E in the repo. They can be done in either order or in parallel.

Everything else in the dev plan (IMU/heel capture, Target-Actual engine, HUD) depends on F.

---

## Current state — what's actually done

Verified by reading the repo on 2026-05-15:

| Area | State |
|---|---|
| D4 user profile (display_name, avatar, sailing/safety) | Code merged `fea2991`. Migration 0015 in repo. `GCS_AVATARS_BUCKET=sailline-avatars` already set in `infra/cloudbuild.yaml`. |
| D4 prod migration apply | Unknown from my vantage — manual step per `docs/migrations.md`. Verify with `alembic current` against prod before next deploy. |
| D4 avatars GCS bucket | Bucket creation is a manual `gsutil mb` step. Verify it exists; if not, the avatar endpoint returns 503 (designed to fail soft). |
| Session A — Capacitor JS adapter | Done. `frontend/src/lib/geolocation.js` is the platform-adaptive layer; `useTrackRecorder` already calls it. |
| Session A — capacitor.config.ts | Committed, Android-only scope, no `cap init` needed. |
| Session A — runbook | `sailline -docs/2026-05-14-android-setup-runbook.md` is the user's playbook. Untouched. |
| Session A — actual native shell | Not generated. `frontend/android/` doesn't exist; `@capacitor/*` packages aren't in `package.json` yet. **This is the remaining work.** |
| Uncommitted change | `infra/cloudbuild.yaml` modified on Windows (not visible from the bash mount — see memory `feedback_bash_mount_unreliable`). Review before commit. |

---

## Session A.fin — finish Capacitor Android setup

**Where:** Windows only. Nothing for me to write in the repo.
**Time estimate:** ~1–2 h including Gradle first-sync (~10 min download).
**Owner:** you, on the Windows box with the Android phone.

**Steps** — follow `sailline -docs/2026-05-14-android-setup-runbook.md` end-to-end. Summary:

1. Prereqs: Android Studio installed, JDK 17, USB debugging on the phone, `adb devices` shows it.
2. From `frontend/`:
   ```powershell
   npm install @capacitor/core @capacitor/android @capacitor/geolocation `
       @capacitor-community/background-geolocation
   npm install --save-dev @capacitor/cli
   npm run build
   npx cap add android
   npx cap sync android
   ```
3. Edit `frontend/android/app/src/main/AndroidManifest.xml` per runbook §4 (permissions + `<service>` for `BackgroundGeolocationService`).
4. `npx cap open android` → ▶ Run.
5. Smoke test: record a 15-min track with the screen locked for 10+ min of it. Pass = continuous trail; fail = gap in the locked window.

**Acceptance:** smoke test passes. Without it Session A is not complete.

**Verify before considering done:**

- `npm test` on Windows passes (the adapter has a unit-tested web branch; Vitest can't run in this sandbox — see memory).
- Production web build `npm run build` still succeeds.
- The persistent foreground-service notification appears while recording and clears on stop.

**Likely friction points** (call out before opening Android Studio):

- The runbook lists the plugin service class as `com.equimaps.capacitor_background_geolocation.BackgroundGeolocationService`. Verify against the plugin's installed `node_modules/@capacitor-community/background-geolocation/README.md` before pasting — package author has been known to refactor.
- Battery-optimisation exclusion (Xiaomi, OnePlus, some Samsungs). If the trail still drops despite "Allow all the time", check Settings → Battery → SailLine → "Don't optimize".

---

## Session E — `/telemetry` viability fix (BACKEND)

**Why this exists:** before any frontend can post to `/api/races/{id}/telemetry`, the endpoint has to actually work. It currently has three issues:

### Issue 1 — column-name bug (blocking)

`backend/app/routers/telemetry.py` line ~248:

```python
INSERT INTO track_points
    (session_id, recorded_at, location, speed_kts, heading_deg, gps_acc_m)
```

The actual column is `position`, not `location` (migration 0002). The endpoint would 500 on the first real call. Tests are fully mocked (no real DB), so this never surfaced.

**Fix:** rename `location` → `position` in the INSERT. One-line change. Add a real-DB integration test (or at least a regression test that asserts the INSERT statement names columns that exist in the schema).

### Issue 2 — auth predicate is pre-D3

`telemetry.py::_verify_race_ownership` does:

```python
SELECT 1 FROM race_sessions WHERE id = $1 AND user_id = $2
```

This is the *old* owner-only auth model. After Session D3, every race-scoped endpoint moved to `app.auth_helpers.race_write_predicate` (boat-crew aware). If the frontend migrates to `/telemetry` as-is, crew members lose the ability to record on shared boats — a D3 regression.

**Fix:** swap `_verify_race_ownership` for `race_write_predicate` exactly the way `tracks.py::_load_race_for_ingest` already does it. Same uid placeholder convention.

### Issue 3 — no mark-rounding, no postprocess trigger

`/track` runs the mark-rounding detector inline, persists new passes to `race_sessions.mark_passes`, and triggers the `race-postprocess` Cloud Run Job when the final mark is rounded. `/telemetry` does none of this.

If the frontend switches to `/telemetry` without parity:

- Auto-stop breaks (depends on `mark_passes` round-trip in the POST response).
- Post-race stats never trigger (the job kicks off when `len(mark_passes) == len(marks)` is first reached, and only from the track handler today).

**Fix:** lift the mark-rounding block from `tracks.py` into a shared helper (`app/services/mark_rounding.py` already has the algorithm — just the persistence + trigger glue needs sharing). Both routers call it on their GPS payload.

### Scope summary

| File | Change |
|---|---|
| `backend/app/routers/telemetry.py` | (a) `location` → `position`. (b) Replace `_verify_race_ownership` with `race_write_predicate`. (c) Call shared mark-rounding helper after GPS insert; surface `mark_passes` + `new_mark_passes` in `TelemetryAck`. (d) Trigger `race-postprocess` job on final-mark crossing. |
| `backend/app/services/track_ingest.py` (NEW) | Extract `_detect_new_passes`, the persistence UPDATE, and the postprocess trigger from `tracks.py` into one function used by both routers. Keeps both endpoints in lock-step. |
| `backend/app/routers/tracks.py` | Refactor to call the shared helper. Behaviour-preserving. |
| `backend/tests/test_telemetry.py` | New tests covering: column-existence regression (use real Postgres via the existing test harness if any; otherwise a SQL parse check), crew-member happy path, mark-pass surface in ack, final-mark postprocess trigger. |
| `backend/tests/test_tracks_router.py` | Verify behaviour unchanged after refactor. |

**Acceptance:**

- `pytest -m "not slow"` green.
- `/telemetry` POST with a GPS-only batch returns 200, inserts rows, emits any new mark passes, and triggers postprocess at final mark — verified by mocked-DB assertions parallel to the tracks router's existing tests.
- Crew member (non-owner) can POST to `/telemetry` for a boat they're on.

**Open question for you:**

The frontend recorder sends `recorded_at`/`speed_kts`/`heading_deg` (track shape). The telemetry schema uses `t`/`sog_kts`/`cog_deg`. Two options:

- (A) Frontend translates on flush (recorder stays in its own shape; adapter at the API boundary).
- (B) Backend accepts both names via Pydantic field aliases on `GpsSample`.

I lean (A). The recorder's local shape is already an internal contract; the wire shape is the API. Mixing aliases on the server confuses the schema. But it's your call.

**Risk:** `executemany` in `/telemetry` is slower than the `unnest` bulk insert in `/track`. Fold the conversion to `unnest` into this session too — small additional diff, real performance win at 1Hz × 100-sample batches.

---

## Session F — frontend GPS capture migration to `/telemetry`

**Depends on:** Session E merged + deployed.

**Goal:** Move `useTrackRecorder`'s flush target from `POST /api/races/{id}/track` to `POST /api/races/{id}/telemetry`. The `/track` endpoint stays alive for the deprecation window so an in-flight race during deploy doesn't lose points.

**Scope**

| File | Change |
|---|---|
| `frontend/src/hooks/useTrackRecorder.js` | Change `apiFetch` URL. Wrap each point in the telemetry batch shape `{ gps: [...] }`. If we picked Option A above, translate field names here. Continue to surface `new_mark_passes` to the auto-stop hook from the ack. |
| `frontend/src/hooks/useAutoStopRecorder.js` | Confirm the ack-shape change is absorbed cleanly. |
| `frontend/src/__tests__/useTrackRecorder.test.js` | Update fixture URLs + payloads. Add a test asserting auto-stop receives `new_mark_passes` from the new endpoint. |
| `frontend/src/lib/geolocation.js` | No change. Adapter contract is the same. |
| `backend/app/routers/tracks.py` | No change yet. `/track` stays alive until the next session. |

**Cutover plan**

1. Ship Session E (server now serves both endpoints with the same semantics).
2. Ship Session F (client switches to `/telemetry`).
3. Observe one real race + one rainy-day desk smoke. If no regressions, schedule a deprecate-`/track` session — additive header-based or commented-out deletion in a separate PR so an old client mid-race never 404s.

**Acceptance**

- One full race recorded end-to-end against `/telemetry` in dev (web), with auto-start, auto-stop, and post-race stats firing exactly as before.
- Offline-queue drain test: airplane-mode for 60s mid-race, points drain to `/telemetry` on reconnect.

**Out of scope for Session F**

- IMU capture (`batch.imu`). Hooked-up empty for now; comes online in the next session after this one (Phase 2 — Browser Sensor).
- Calibration UI. Same — its own ½-session per the dev plan.

---

## Why this order (and not "go straight to IMU")

The dev plan's "Suggested next-session ordering" item #1 is "Adapt frontend GPS capture to `/telemetry`." I'm proposing the same destination but adding Session E as a prerequisite because the endpoint isn't viable as-shipped. Skipping E means the first time a real client posts to `/telemetry` we get a 500 from Postgres, plus crew members silently lose access to shared boats.

Without Session E, the migration looks done locally (mocked tests are green) but blows up in prod the moment a real device flushes — same trap as the bash-mount memory note: tests passing isn't the same as the thing being correct.

---

## What I deliberately did NOT plan

- **iOS native build.** Out of reach until you have a Mac; existing capacitor scope is Android-only by decision.
- **Permission downgrade detection** (the Android "Allow all the time" → "While using" silent fail). Real concern, but a follow-up after one real race surfaces whether it bites in practice.
- **Removing `/track`.** Premature. Keep both endpoints alive through the cutover.
- **D4 race-entry pre-fill** (Mac/Bermuda forms using the sailing-and-safety bundle). Real but not urgent — no upcoming distance race.

---

## Tech debt flagged while reading

These came up while I was paging through; none block the above sessions but worth a backlog note:

- `backend/app/routers/telemetry.py` has no real-DB test — that's how the column-name bug survived. Worth a CI smoke that spins up Postgres for at least one INSERT per router (or a schema-introspection check).
- `tracks.py` and a future `telemetry.py` will both call a shared `_detect_new_passes` + `trigger_race_postprocess` block. Two copies will drift; the Session E extraction prevents that.
- `phrf_cert.py` has a stray trailing-whitespace line at EOF (visible in `git diff`). Trivial cleanup, not urgent.
- D4 summary deployment items (avatars bucket, migration 0015) are not visibly checked off anywhere. Worth one `gcloud storage buckets describe` + `alembic current` against prod to confirm before next deploy.

---

## How to use this doc

When you sit down next session, pick one of the three top-level headers (A.fin / E / F), open the corresponding section, and execute. Each is sized to fit a single context window. I'll re-verify state at session start either way — don't trust this doc blindly (per `feedback_verify_state` memory).
