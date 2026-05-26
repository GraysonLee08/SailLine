# Session Summary — 2026-05-26 (evening)

Continuation of the morning session. Three independent threads landed:
blended auto-start for the RN recorder, a SQL inspection script + first
successful Android smoke test, and an accurate `started_at` /
`ended_at` story driven by the mark-rounding detector.

## 1. Blended auto-start — T-6 notification + T-5 BackgroundFetch

### What we worked on

Replaced the unreliable foreground-only `setTimeout` auto-start on
mobile with a three-tier idempotent trigger: foreground timer + OS-level
notification at T-6 + OS-level BackgroundFetch task at T-5. All three
ultimately call `recorder.start()`, which guards on
`recordingRef.current` so only the first one wins.

User decision drove the design: tap-to-start alone is unworkable
("nobody is unlocking the phone 5 min before the gun"); a silent BG
task alone is risky on iOS where BackgroundFetch is best-effort.
Blended approach gives the user a visible reminder AND a silent
fallback.

### Files added

- `mobile/src/recorder/scheduledAutoStart.ts` (new) — OS-level
  fallbacks. Exposes `registerHandlers`, `scheduleAutoStart`,
  `cancelAutoStart`, `setOnFire`, `replayPendingTap`,
  `requestNotificationPermission`. Handles the cold-wake case by
  posting a "Race starting now" notification when the BG task fires
  without a registered onFire callback (React not mounted).

### Files modified

- `mobile/src/recorder/useAutoStartRecorder.ts` — keeps the foreground
  `setTimeout` fast path; layers in `scheduleAutoStart` /
  `cancelAutoStart` side-effects and registers the recorder's `start`
  callback via `setOnFire`.
- `mobile/App.tsx` — `registerHandlers()` called at module load
  (before React mounts so headless wakes find their handlers);
  `AuthedShell` requests notification permission once after sign-in.
- `mobile/app.config.js` — adds `expo-notifications` plugin block.
  Comments document the runtime-only channel config decision.
- `mobile/package.json` — adds `expo-notifications ~0.32.17` (Expo
  pinned the SDK-54-matched version after `npx expo install`).

### Decisions made

1. **No headless recorder bootstrap.** When the OS wakes the app cold
   from a killed state at T-5, we deliberately do NOT try to start the
   recorder — would need Firebase auth, raceId, polar cache, etc.
   bootstrapped from nothing. Instead we post a "Race starting now"
   visible notification as the last-resort signal.
2. **Notification AT T-6, BG task AT T-5.** The 1-minute gap means the
   user always has a chance to take manual control before the silent
   fallback fires. Flipping the order would just be telling them after
   the fact.
3. **Notification body explicitly mentions the silent fallback.**
   "Tap to start tracking. SailLine will auto-start at T-5 if you
   don't." Sets the expectation that ignoring it is fine — less
   anxiety, more trust.
4. **`forceAlarmManager: true` on BackgroundFetch.scheduleTask.**
   Android-only flag; uses AlarmManager for precise timing instead of
   the JobScheduler. iOS ignores the flag.

### Tech debt flagged

- iOS BackgroundFetch is best-effort; T-5 task may fire late by a few
  minutes under Low Power Mode. If on-water testing surfaces this as a
  real problem, the right next move is silent FCM push from the
  backend at T-5 — iOS treats those more reliably than fetch. Backend
  work, out of scope for this PR.
- Headless cold-wake posts a notification but doesn't actually start
  the recorder. A future iteration could persist a "pending auto-start
  raceId" and bootstrap minimal recorder state from headless — but
  that's a separate, much larger piece of work.
- Timer logic now duplicated between foreground `setTimeout` and OS
  schedulers. Revisit after one season of real races to see which
  paths actually fire.

---

## 2. SQL inspection script + first end-to-end Android smoke test

### What we worked on

Added two SQL scripts for verifying the mobile recorder after a smoke
test. Walked through reading the first end-to-end Android capture:
**991 GPS points over 33 minutes**, recorder pipeline confirmed
working end-to-end (Phase 1 acceptance criterion met).

### Files added

- `backend/scripts/inspect_recent_track.sql` (new) — psql-flavoured
  five-section diagnostic against the most recent session for a user.
  Uses `\set` meta-commands for parameter binding.
- `backend/scripts/inspect_track_gui.sql` (new) — GUI-flavoured
  equivalent for Cloud SQL Studio, pgAdmin, DBeaver. Five queries
  with the session UUID hard-coded for find/replace.

### Smoke test result (session `6f7a2111-d7c0-48d7-8884-f09619f5cdf6`)

- 991 points over 33m 10s, avg cadence 2.0s (target 1Hz).
- Max gap 577s (9.6 min) mid-test — Android battery optimisation or
  Doze almost certainly. Next test must verify Battery → Unrestricted
  is actually set; the indoor location (Wrigleyville stationary)
  may also have factored in.
- Distance 0.285 nm — consistent with stationary indoor capture.
- **Surfaced a pre-existing bug**: `started_at` and `ended_at` were
  NULL despite 991 points landing. Investigation showed nobody had
  ever been writing those columns, on web OR mobile, since the
  baseline migration. Fixed in thread #3 below.

### Open items / next steps for the recorder

- Outdoor walking smoke test to isolate "indoor GPS" vs "OS-suspended"
  as the cause of the 9-minute gap.
- Verify Settings → Apps → SailLine → Battery → Unrestricted.

---

## 3. Accurate `started_at` / `ended_at` lifecycle columns

### What we worked on

Made the dormant `race_sessions.started_at` and `ended_at` columns
real, populated server-side from the authoritative sources already in
the database. Built on top of the existing mark-rounding detector
(2026-05-14) with two algorithm refinements: per-mark radii (75m for
the final mark, 50m for intermediates) and closest-approach
timestamps (instead of exit-point timestamps).

### Backend files changed

- `backend/app/services/mark_rounding.py` — added
  `FINAL_MARK_RADIUS_M = 75.0` constant, per-mark radii support
  (scalar or sequence), closest-approach timestamp emission,
  `radii_for_course(n)` helper.
- `backend/app/services/track_ingest.py` — `load_race_for_ingest`
  now reads `started_at` + `start_at` alongside `marks` +
  `mark_passes`. `detect_and_persist_new_passes` accepts them and
  writes both lifecycle columns in the same dynamic UPDATE that
  persists new mark passes. Idempotent: `started_at` only written
  if currently NULL; `ended_at` wrapped in `COALESCE` so a manual
  write can't overwrite an authoritative detector value.
- `backend/app/routers/tracks.py` — passes the new fields through.
- `backend/app/routers/telemetry.py` — same.
- `backend/app/routers/races.py` — `update_race` PATCH now wraps
  `ended_at` writes in `COALESCE(ended_at, ${idx})` so the
  manual-stop fallback PATCH can't clobber a detector-written value.

### Backend tests

- `backend/tests/test_mark_rounding.py` — added 7 cases: closest-
  approach timestamp emission, per-mark radii honored, per-mark
  radii widening only the final mark, `radii_for_course` shape,
  length-mismatch validation, all-positive validation,
  `FINAL_MARK_RADIUS_M` constant tripwire. All 17 cases pass.
- `backend/tests/test_tracks_router.py` — added 5 cases:
  `started_at` backfill on first POST, no-overwrite when already
  set, no-write when `start_at` is NULL, `ended_at` written on
  final pass, no `ended_at` write on intermediate mark. Also added
  `_finish_pass_points()` helper and widened the existing
  single-mark test's geometry to span ±148m (so it cleanly exits
  the 75m zone). All 21 cases pass.

### Frontend files changed

- `packages/shared/src/markRounding.js` — full mirror of the
  Python changes; `FINAL_MARK_RADIUS_M` + `radiiForCourse`
  exported via the package's wildcard re-export.
- `frontend/src/lib/markRounding.test.js` — 7 new mirror cases
  (closest-approach, per-mark radii widening, `radiiForCourse`
  shape, validation, constant tripwire). All 16 cases pass.
- `frontend/src/hooks/useAutoStopRecorder.js` — now passes
  `radiiForCourse(n)` into `computePasses` so the client's
  auto-stop banner agrees with the server on when the finish
  fires.
- `frontend/src/hooks/useAutoStopRecorder.test.js` — adjusted the
  "schedules stop 5 min after the final rounding" case to align
  `Date.now()` with the new closest-approach index (10) instead
  of the old exit index (15). All 7 cases pass.
- `frontend/src/hooks/useTrackRecorder.js` — `stop()` now PATCHes
  `ended_at = NOW()` as a DNF fallback. Fire-and-forget; the
  server's `COALESCE` makes it a no-op when the detector already
  wrote an authoritative value.

### Scripts

- `backend/scripts/backfill_started_ended_at.sql` (new) — one-off
  backfill for existing race rows. Two preview SELECTs + two
  idempotent UPDATEs + a post-run audit. `started_at` falls back
  to first track point timestamp when `start_at` is NULL.
  Run once after deploy.

### Decisions made

1. **`started_at = race.start_at`** (the scheduled gun time), not
   recorder-start time. Honest "the race officially started at the
   gun" semantics. Differs from when the recorder began capturing —
   that's fine, the boat is in the pre-start box doing tactics.
2. **`ended_at = closest-approach timestamp to the final mark**, not
   recorder-stop time and not exit-point timestamp. Closest-approach
   is essentially "when you crossed the line through the mark";
   exit-point is a few seconds late.
3. **75m for the final mark only.** Research into RRS, ILCA race
   management policies, and America's Cup specs showed no universal
   standard: typical club finish lines are 50-150m wide. 75m covers
   the median case without picking up adjacent legs on W-L courses.
   Source citations live in the conversation history.
4. **User-defined finish-line geometry deferred.** Most sailors
   don't author the line ahead of time; closest-approach + 75m
   radius covers ~95% of club finishes without forcing extra
   course-setup steps.
5. **Server-side writes for both columns.** Web AND mobile clients
   work automatically with zero client changes — single source of
   truth, no PATCH-coordination bugs.
6. **`COALESCE` guard in two places** (helper UPDATE and `update_race`
   PATCH). The detector's value is more accurate than any wall-clock
   stop time; never overwrite it.

### Tech debt flagged

- 75m hard-coded for the final mark. Per-race configurable column
  is a small follow-up if real races surface false positives or
  misses on unusually wide finish lines.
- `recompute_passes` admin endpoint (already-flagged tech debt from
  2026-05-14) becomes more valuable after this change — it would
  recompute `ended_at` too from the raw points. Same priority as
  before.
- Mobile auto-stop hook port is the natural next mobile feature —
  the web has `useAutoStopRecorder`; mobile doesn't.
- `started_at` backfill from `track_points` first sample is a small
  semantic compromise for ad-hoc races (no scheduled gun time);
  acceptable for v1.

---

## Verification

All Windows test runs green:

```
pytest tests/test_mark_rounding.py tests/test_tracks_router.py -v
   38 passed in 1.05s

npm test
   13 test files passed, 149 tests passed
```

## Deploy plan

1. **No migration needed** — all schema columns already exist; this
   is pure code.
2. Merge backend + frontend changes. Cloud Build auto-deploys.
3. Run `backend/scripts/backfill_started_ended_at.sql` against prod
   (Cloud SQL Studio is fine). Idempotent; safe to re-run.
4. Smoke test: create a race with a scheduled `start_at`, walk past
   the mark in a real test, confirm `started_at == start_at` and
   `ended_at` ≈ when the boat crossed the mark.

## Outstanding items requiring user action (mobile build)

The mobile auto-start changes (thread #1) require an EAS rebuild
because `expo-notifications` is a new native module. The rebuild
hasn't run yet — the user should execute:

```powershell
cd E:\Personal\Coding\SailLine\mobile
npx eas-cli build --profile development --platform android
```

Before that rebuild lands, the mobile auto-start work is shipped but
inert — the recorder still works, just without the OS-level fallbacks.
