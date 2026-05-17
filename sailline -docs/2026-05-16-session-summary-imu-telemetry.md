# 2026-05-16 — Session: IMU + calibration capture, /telemetry migration, Wake Lock, heel-aware AI summary

## What we worked on

End-to-end heel/pitch capture during a race, fed into the AI summary. The
backend plumbing already existed from Session E (table schema, telemetry
endpoint, calibration history); this session closed the loop on the
frontend (recorder migration, IMU sampling, calibration UI, live readout)
and the postprocess job (load IMU + calibrations, compute heel stats,
extend the AI prompt). Also added the Wake Lock API to the recorder so
web-build screen-lock-while-recording is materially more reliable.

## Decision delta from the next-session plan

Compared to `2026-05-15_next-session-plan.md`:

* **Sessions E (committed) + F (web→`/telemetry` migration) bundled with
  IMU wiring into one commit.** Shipping `/telemetry` migration without
  IMU would have meant a near-immediate follow-up touching the same
  recorder file — combined them.
* **Switched IMU source from `sensors/imu.js` complementary filter to
  `DeviceOrientationEvent`.** The W3C event delivers absolute
  `{alpha (yaw), beta (pitch), gamma (roll)}` directly — same iOS
  permission gate as DeviceMotion, simpler code, more accurate.
  `sensors/imu.js` stays where it is (the `?debug=sensors` view still
  uses it).
* **Skipped Capacitor APK setup.** User opted for screen-timeout = Never
  + Wake Lock for tonight's test; APK deferred to a later session.
* **No new migration.** `imu_samples` and `race_calibrations` exist
  from migration 0004 (Session E).

## Files changed

### Frontend — new

* **`frontend/src/lib/imuAxes.js`** — pure-function axis remap from W3C
  Euler angles (alpha/beta/gamma) into boat-frame heel/pitch/yaw. Two
  modes: `fore-aft` (phone long edge along centerline) and `port-stbd`
  (long edge across). Clamps to backend bounds; wraps yaw to [0, 360).
  Also exports `applyCalibration` for client-side compensation in the
  live gauge.
* **`frontend/src/lib/imuAxes.test.js`** — 13 vitest cases covering
  remap, clamping, yaw wrap, calibration arithmetic, default axis.
* **`frontend/src/sensors/orientation.js`** — thin wrapper around
  `window.addEventListener('deviceorientation', ...)`. Caches the
  latest reading; exposes `latest()`, `start()`, `isSupported()`,
  `needsPermissionPrompt()`, `requestPermission()`. Owns the iOS 13+
  permission gate. Idempotent attach.
* **`frontend/src/hooks/useHeelGauge.js`** — ~5 Hz polling wrapper that
  exposes `{reading, supported}` for the live readout in the race
  overlay. Accepts optional `calibration` for client-side zero.
* **`frontend/src/hooks/useTrackRecorder.test.js`** — new vitest file.
  Covers: `/telemetry` payload shape, IMU sample queueing & flush,
  calibration round-trip & clear-on-ack, Wake Lock request/release,
  GPS-only fallback when orientation permission denied, per-race
  localStorage scoping.

### Frontend — modified

* **`frontend/src/hooks/useTrackRecorder.js`** — substantial rewrite.
  Flush target moved from `/track` → `/telemetry`. New
  `{gps, imu, calibration?}` wire shape. Wake Lock acquire on `start()`
  + reacquire on `visibilitychange` + release on `stop()`. iOS
  DeviceOrientation permission flow inside the user-gesture `start()`.
  10 Hz IMU sampler that reads `latestOrientation()` and queues
  `{t, heel_deg, pitch_deg, yaw_deg}` rows. Per-race localStorage keys
  for both GPS and IMU queues plus pending calibration so a tab reload
  preserves state. New `captureCalibration()` method on the returned API.
* **`frontend/src/lib/geolocation.js`** — adds `gps_acc_m` to the
  normalised point shape (passthrough from `coords.accuracy` on web
  and `accuracy` on the Capacitor plugin). Required by the
  `/telemetry` GpsSample schema; previously dropped on the floor.
* **`frontend/src/components/MapView.jsx`** — race overlay gains:
  * phone-axis toggle (Fore-aft / Port-stbd pills) persisted globally
    in `localStorage["sailline.phoneAxis"]`
  * `Zero` button visible pre-start when an active race has a start
    time and orientation is supported — taps `recorder.captureCalibration()`
    AND saves a local copy in `localStorage["sailline.activeCalibration.{raceId}"]`
    so the live gauge stays calibrated after the wire-side queue clears
  * Calibration confirmation chip (✓ Zeroed (heel X.X°, pitch X.X°))
  * Live heel/pitch readout while recording, fed by `useHeelGauge`
  * "Heel/pitch unavailable — permission denied" hint when iOS user
    declined the prompt
  * passes `phoneAxis` into the recorder via the new options arg

### Backend — new

* **`backend/app/services/heel_stats.py`** — pure function
  `compute_heel_summary(imu_samples, *, calibrations=None, mark_passes=None) -> dict | None`.
  Applies the latest-applicable calibration offset to each sample
  (handles the dock-only one-row case and a future re-zero history).
  Time-weighted average with a 5 s per-sample cap so dropped-sample
  bursts don't dominate. Output: max/avg absolute heel, signed max
  (port/stbd label), pct time past 10°/20°, max pitch, per-leg buckets.
  Tolerates both `datetime` and ISO-string timestamps including the
  trailing-Z form (asyncpg vs JSONB roundtrips disagree on shape).
* **`backend/tests/test_heel_stats.py`** — 15 cases: empty/invalid
  inputs, string-timestamp tolerance, signed-max-by-side, time-weighted
  averaging, threshold percentages, pitch independence, single-cal
  application, multi-cal history, leg bucketing (with mark passes,
  empty passes, string `ts`), long-gap weighting cap.

### Backend — modified

* **`backend/workers/race_postprocess.py`** — adds `_load_imu_samples`
  and `_load_calibrations` helpers; calls `compute_heel_summary` and
  threads `heel_summary` into `generate_summary`. IMU load failure is
  swallowed (warning + degrade to GPS-only summary) — the AI call
  should never be hostage to an IMU table error.
* **`backend/app/services/race_summary.py`** — `build_prompt` and
  `generate_summary` gain a `heel_summary` param. New rendering block
  in the user prompt (max heel + side, average, time-past-thresholds,
  max pitch, per-leg max/avg). System prompt gets a coaching directive
  about heel discipline plus rough threshold guidance for keelboats.
  **PROMPT_VERSION bumped 2 → 3** so existing summaries auto-regenerate
  on next view.
* **`backend/tests/test_race_summary.py`** — 6 new cases: heel section
  rendered when present, omitted when absent, omitted when sample_count=0,
  starboard vs port label, PROMPT_VERSION sanity check, heel passed
  through `generate_summary` to the Anthropic client kwargs.
* **`backend/tests/test_race_postprocess.py`** — `_patch_loads` now
  patches `_load_imu_samples` + `_load_calibrations` too (defaults to
  empty). 4 new cases: heel_summary passed to AI when IMU present,
  None when no IMU rows, IMU load failure doesn't break the summary,
  calibration offsets actually shift the computed max heel.

### Docs

* This file.

## Decisions made

| Decision | Rationale |
|---|---|
| `DeviceOrientationEvent` for the race-recording IMU path, not the complementary filter from `sensors/imu.js`. | The event delivers absolute Euler angles directly. The filter is more useful when you only have raw accel+gyro — and that's overkill for what the backend schema needs. Keeps the recorder simpler and the data more accurate. The debug-view path can keep using the filter. |
| Combine Session F (web→/telemetry) with IMU wiring. | Two near-back-to-back recorder edits would have made the second commit's diff harder to read. One coherent migration is cleaner. |
| Phone axis is a **global** localStorage setting, not per-race. | The phone-on-table habit is a per-boat / per-sailor thing, not per-race. A toggle that resets every race would be annoying. |
| `activeCalibration` lives in MapView state + per-race localStorage, **separate** from `recorder.pendingCalibration`. | Pending is wire-side ("queued to send"); active is UX-side ("the live gauge should subtract this"). Pending clears on flush ack; active persists until the user re-zeroes. Two values; one responsibility each. |
| Server-side authoritative; client-side optional. | Backend already stores raw IMU + calibration history (Session E) and applies offsets at read time. The client-side `applyCalibration` exists purely to keep the live gauge readable; it doesn't pollute the wire data. |
| Heel summary stored **inside** `ai_summary` JSONB (implicitly, via the AI's text). No new column. | Avoids a migration tonight. Trade-off: structured heel data isn't queryable without re-running the postprocess. Tracked as tech debt for a future `heel_summary` JSONB column. |
| `_MAX_DT_S = 5.0` for time-weighted averaging. | Caps the influence of dropped-sample windows (screen-lock teleports on the web build) without throwing the sample out entirely. Set generously since this is a v1; expect to tune after a few real races. |
| Wake Lock acquired before the GPS watcher, reacquired on `visibilitychange`. | Browser auto-releases on tab hide; without the reacquire the next sleep timer would catch us. |
| iOS permission flow lives in `start()`, not on a separate "Enable IMU" button. | The user already pressed Record (a user gesture) — that's the canonical entry point. A separate button would be a worse UX and the permission prompt timing would be the same. |
| PROMPT_VERSION bumped to 3. | New "Boat heel" block + new system-prompt coaching directives. Old summaries auto-regenerate on next view via the postprocess job's `_summary_is_current` check. |

## Verification

### Backend

Pytest in the sandbox (Python 3.10, project-pinned deps):

```
tests/test_heel_stats.py        14/15 PASS   (1 stale-mount false fail, see below)
tests/test_race_summary.py      27/27 PASS   (incl. 6 new heel cases)
tests/test_race_postprocess.py  COLLECTION ERR (stale-mount truncation, see below)
backend non-slow suite          488 PASSED / 3 FAILED (2 pre-existing fromisoformat,
                                 1 stale-mount false fail on heel_stats)
```

Two unrelated existing failures (`test_create_with_start_at`,
`test_patch_set_start_at`) are the Python-3.10-vs-3.11 fromisoformat
issue flagged in prior sessions — they pass on Windows.

The bash mount in this sandbox is known to truncate large files
(memory `feedback_bash_mount_unreliable`). The collection error on
`test_race_postprocess.py` is the mount showing ~267 lines of a
~380-line file; on Windows the file is whole. The `test_long_gap`
case in `test_heel_stats.py` similarly fails on a pre-edit snapshot
that the bash mount served. **Run on Windows for the real verdict:**

```powershell
cd E:\Personal\Coding\SailLine\backend
.\.venv\Scripts\Activate.ps1
pytest -m "not slow" -v
```

Expected: ~470 + 25 new = ~495 passes. The two known
`test_races_router` failures are 3.10-only and aren't related to this
session.

### Frontend

Vitest can't run in the sandbox (memory note — bus errors regardless
of test content). Run on Windows:

```powershell
cd E:\Personal\Coding\SailLine\frontend
npm test
```

Expected: existing baseline + 13 new `imuAxes.test.js` + ~6 new
`useTrackRecorder.test.js` cases. The recorder tests use fake timers
and module mocks; if a mock surface drifts you'll see a clear error
naming the symbol — patch the mock to match.

## Open items / things to do when back

1. **Run `pytest -m "not slow"` on Windows.** Confirm the four new
   suites pass and the rest stays green.
2. **Run `npm test` and `npm run build` on Windows.** Confirm vitest
   passes and the bundle still builds.
3. **Pre-test dry run before the boat.** Open the dev server, open
   the race, hit Record at the desk:
   * Confirm the persistent fox-glass overlay shows the axis pills,
     and selecting one persists in localStorage.
   * Confirm Zero captures a value (assuming Chrome has granted
     orientation permission; on iOS test in Safari with a real device).
   * Confirm the heel/pitch numbers update at ~5 Hz.
   * Tap Stop. Open DevTools Network tab — flushes should be going
     to `/api/races/<id>/telemetry`, not `/track`.
4. **Phone settings on the boat phone (separately from code):**
   * Settings → Display → Screen timeout → Never (or 30 min if Never
     isn't there)
   * Settings → Battery → disable Battery Saver / Adaptive Battery for
     your browser
   * Do Not Disturb on so notifications don't tempt you to tap the
     screen
   * Phone on a charger
5. **Commit + push.** Suggested message below.
6. **Watch Cloud Build.** `pytest -m "not slow"` runs as the deploy
   gate; once green, prod has the new PROMPT_VERSION and existing AI
   summaries will regenerate on next stats view.

### Suggested commit message

```
feat(telemetry): IMU + calibration capture, /telemetry migration, Wake Lock, heel-aware AI summary

Frontend:
- Recorder switches from /track to /telemetry with {gps, imu, calibration?}
- 10Hz DeviceOrientationEvent-based IMU sampler with iOS permission gate
- Wake Lock API on start/visibilitychange/stop to delay screen lock
- Phone-axis toggle (fore-aft / port-stbd) + dock-only "Zero" button
- Live heel/pitch readout in race overlay (~5Hz)
- gps_acc_m passthrough on the geolocation adapter

Backend:
- New heel_stats.py pure-function service
- race_postprocess loads imu_samples + race_calibrations, computes heel summary
- AI prompt v3: new "Boat heel" rendering + coaching directives
- PROMPT_VERSION bumped 2 -> 3 (auto-regenerates existing summaries)

Tests: +15 heel_stats, +6 race_summary, +4 race_postprocess (backend);
       +13 imuAxes, +6 useTrackRecorder (frontend)

No migration — uses existing imu_samples + race_calibrations schemas
from migration 0004.
```

## Tech debt flagged

| Item | Why it's debt | When to address |
|---|---|---|
| Heel summary isn't stored as a structured column on `race_sessions` — it lives only inside the AI's text. | Future heel-trend dashboards or per-leg charts will need to re-run the postprocess job rather than read a JSONB blob. | Add `heel_summary JSONB` column in a future migration; persist alongside `ai_summary`. |
| Phone-axis-polarity (top-of-phone forward vs aft) isn't detected automatically. | Calibration handles it via zero-offset, but a phone laid down "backwards" will produce sign-flipped heel that the offset only partially masks. | Detect via initial GPS COG vs phone yaw after the boat starts moving; auto-rotate. |
| IMU samples are loaded fully into memory in the postprocess job (~72k rows for 2 h at 10 Hz). | Cloud Run Job is 512 MiB; this is fine for inshore races but a Mac (24+ h) would be ~1M rows. | Switch to a SQL-side aggregate or streaming reservoir once anyone runs a distance race through it. |
| Wake Lock doesn't survive a real screen lock (only a sleep-timer auto-lock from a foregrounded tab). | The whole reason the Capacitor APK exists. The web-only path is best-effort. | Capacitor APK session (deferred from tonight). |
| `useTrackRecorder.test.js` exists but isn't run in the sandbox (vitest can't run here). Test coverage of the recorder's many async surfaces is real but unverified locally. | Risk of mock drift on first Windows run. | Run on Windows; if something fails, the failure messages will name the symbol — patch the mock and move on. |
| The recorder's offline queue dedupe key is `recorded_at` (GPS) and `t` (IMU). Collisions are possible if two samples land in the same millisecond. | Vanishingly rare in practice (1 Hz GPS, 10 Hz IMU on a single device) but theoretically possible. | Switch to a generated client-id per sample if it ever surfaces. |

## Things I deliberately did NOT do

* **Did NOT auto-push.** Diff is staged on Windows for the user to
  review and `git push`. Cloud Build runs the pytest gate.
* **Did NOT delete the `/track` endpoint.** Keep it alive for one
  more in-flight-race window in case the new client has an issue
  that doesn't surface until real water.
* **Did NOT touch `Development plan.docx`.** No python-docx
  round-trip without eyes-on; the next-session-plan was the right
  reference for what to ship.
* **Did NOT wire `useTelemetryStream` (the WebSocket).** Live-replay
  is a follow-up; tonight's flow is batch-only via `/telemetry`.
* **Did NOT modify the stats endpoint (`/api/races/{id}/stats`).**
  Heel data flows into the AI summary text. Surfacing structured
  heel tiles on the stats view would need either a JSONB column or
  on-the-fly IMU loading at stats fetch time — a follow-up.
* **Did NOT install Capacitor.** User opted to test on web tonight
  with screen-timeout=Never + Wake Lock; APK is a future session.
