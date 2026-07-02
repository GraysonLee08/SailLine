# Race-Night Review — Beer Can 7.1.2026

**Date:** 2026-07-02
**Race:** `f2a4cb69-2750-48f9-96af-1674c42c9b20` (Beer Can 7.1.2026, start 2026-07-02T00:15Z)
**Status:** Findings + remediation plan. No code written — awaiting plan approval per project rules.

---

## 1. What the data shows

From `race_sessions` (Cloud SQL Studio export) and Cloud Run logs:

| Field | Value | Meaning |
|---|---|---|
| `started_at` | 00:15:00Z | Auto-start DID fire on the backend |
| `ended_at` | **NULL** | Race never ended, server-side |
| `mark_passes` | **[]** | Zero marks detected — including the start |
| `ai_summary` / `wind_snapshot` / `heel_summary` / `performance_summary` | all empty | Postprocess never ran |
| `detector_state.min_dist` | 389.76 m | Closest recorded approach to mark 1 (SA7) |
| `detector_state.min_ts` | 00:13:27Z | …recorded **2 min before the gun** |
| `detector_state.departing` | 0 | Departure sequence never accumulated |

Track points ARE in the DB (`updated_at` advanced through 01:16Z) — the race data is recoverable.

## 2. Root cause per observation

### 2.1 No view swap on auto-record
Auto-start fires and `recorder.start()` runs (`mobile/src/recorder/useAutoStartRecorder.ts:207-220`), but the only navigation to `/recording` is an effect that lives on the **home screen** (`app/(app)/index.tsx:204-206`). App backgrounded or on another screen → no navigation. The 2026-06-30 banner only renders once you're already on the recording screen.

### 2.2 Start mark "missed" + repeated notifications
Two stacked problems in the CPA detector (`backend/app/services/track_ingest.py`):

1. **Pre-start poisoning.** The detector doesn't filter samples before `start_at`. A 390 m minimum at 00:13 became the persisted running minimum; the real start crossing at gun time reset the `departing` counter and the pass never fired.
2. **Wrong model.** The start is a single point with a 250 m radius. A beer-can start line extends an indeterminate distance from the mark, perpendicular to the wind — a legal start can easily be >250 m from the committee mark. Radius-around-a-point cannot represent this.

Because mark 1 never passed, the sequence never advanced → every subsequent mark was "missed" → the missed-mark prompt re-armed and re-fired on its 3-minute interval all race.

### 2.3 No auto-stop at finish
Auto-finish sets `ended_at` only when the **final** mark passes (`track_ingest.py:365-371`). Cascading failure from 2.2 — it could never fire.

### 2.4 Track cleared / no comparison view
`handleStop` in `mobile/app/(app)/recording.tsx:255-274` tears down the recorder and does `router.replace("/")`. No post-race map exists on mobile; `race-review/[id].tsx` is stats/AI only. The web `RaceStatsView` shows marks + track but not the computed route. Nothing anywhere renders track-vs-route post-race.

### 2.5 No AI summary
Postprocess is gated on `ended_at`. The manual Stop did call `POST /api/races/{id}/end`, but it's wrapped in a silent best-effort `try/catch` (recording.tsx:258-263) and evidently failed — no retry, no error surfaced, `ended_at` stayed NULL, pipeline never triggered. (The old exit-2 job-args bug is confirmed fixed; this is a different failure.)

## 3. Target UX (from interview, 2026-07-02)

1. **Start/finish = virtual line** through the start mark, perpendicular to the wind, extending far to each side. Line bearing from forecast wind at gun time, with manual override in the race editor.
2. **Mark detection = fully automatic**, geometry-based. Marks gain a **rounding side** (port/stbd); a gate ray extends from each mark on the required side; crossing the ray = passed. No confirmation taps in the normal case.
3. **Safety net:** ONE actionable notification per mark, only when the backend record and the phone's GPS genuinely disagree. Actions: "Mark passed" / dismiss. Never repeats.
4. **Minimal notification set, all actionable where relevant** (must render actions on Garmin via Connect smart notifications):
   - "Recording started" — informational
   - "Race completed" — informational
   - "Start recording" — only if auto-record failed to fire; action: Start
   - "Mark missed" — safety net above; action: Mark passed
5. **Stop (auto or manual) → immediate debrief screen:** full map with recorded track (red) vs computed route (blue) + marks, time scrubber; leg-by-leg stats (tap to zoom); AI recap with a visible "generating…" state; tactician-call replay pins on the track. Track is never cleared.

## 4. Remediation plan (proposed — awaiting approval)

### Phase 0 — Recover last night's race (today, no code)
1. Set `ended_at` from the last track point (Cloud SQL Studio):
   ```sql
   UPDATE race_sessions
   SET ended_at = (SELECT max(recorded_at) FROM track_points WHERE session_id = 'f2a4cb69-2750-48f9-96af-1674c42c9b20')
   WHERE id = 'f2a4cb69-2750-48f9-96af-1674c42c9b20';
   ```
   (Column verified: `track_points.recorded_at`.)
2. Execute postprocess (PowerShell):
   ```powershell
   gcloud run jobs execute race-postprocess --region=us-central1 `
     --args="-m,workers.race_postprocess,--race-id,f2a4cb69-2750-48f9-96af-1674c42c9b20,--force"
   ```
   Mark passes will still be empty (detector data is what it is), but stats/wind/AI summary should generate.

### Phase 1 — Stop the bleeding (small, independent fixes)
- **1a. Global recording guard (mobile):** move the `recording → /recording` navigation from the home-screen effect into the root layout so it fires from any screen and on app foreground/cold start. Fixes 2.1.
- **1b. Un-silence `endRace` (mobile):** retry with backoff, queue for retry on next connectivity if offline, and show an error state instead of swallowing. Fixes the 2.5 trigger.
- **1c. Server-side sweep (backend):** scheduled job ends any race still open N hours after `start_at` (using last track point) and triggers postprocess. Belt-and-braces so a dropped `/end` call can never orphan a race again.
- **1d. Pre-start filter (backend):** detector ignores samples before `start_at` and validates persisted `detector_state` against `start_at` on resume. Fixes the poisoning even before the line model lands.
- **1e. Notification de-dup:** missed-mark prompt fires at most once per mark. Kills the nagging immediately.

### Phase 2 — Detection model rebuild
- **2a. Rounding side — data + setup UI.** Marks JSONB gains `rounding` ("port" | "starboard"); race_sessions gains `start_line_bearing_override`. Race setup (web `RaceEditor.jsx`) becomes a per-mark row matching the race-book format: Mark name, Latitude, Longitude, **Leave Mark to** (dropdown, Port/Starboard only), Description — all mandatory except Description. Save validation rejects marks without a rounding side. Start and Finish rows carry it too (it determines which end of the start/finish line matters). The 24 MORF library marks / 64 course presets (`frontend/src/lib/morfMarks.js`, `morfCourses.js`) need rounding sides added or the preset flow prompts for them — existing races without `rounding` keep working via the CPA fallback until edited.
- **2b. Start/finish line crossing:** perpendicular-to-wind line through the start mark (forecast wind at `start_at`, manual override). Pass = segment crossing after the gun, within a sanity distance of the mark. Same model for the finish.
- **2c. Gate-ray rounding detection:** ray from each mark on the required side; pass = track segment crosses the ray in the correct leg context. Replaces radius/CPA as the primary signal; CPA kept as the ambiguity detector for the safety-net notification.
- **2d. Safety-net notification:** single actionable "Mark missed?" only when gate + CPA disagree. Verify action-button rendering on the Fenix via Garmin Connect before calling it done.
- **2e.** Bump detection state versioning; full pytest coverage with synthetic tracks replaying last night's geometry (we have the real track to test against).

### Phase 3 — Debrief experience
- **3a. Debrief screen (mobile):** map with track vs computed route + marks, time scrubber, leg stats, AI recap with "generating…" polling state, tactician-call pins. Stop navigates here, always.
- **3b.** "Race completed" and "Recording started" informational notifications wired to open the debrief / recording view.
- **3c. Web parity:** add computed-route layer to `RaceStatsView`.

### Suggested order
Phase 0 today. Phase 1 next (each item is small and shippable alone). Phase 2 before the next beer can if possible — 1d alone may be enough to survive one more race night. Phase 3 after.

## 5. Technical debt flagged
- 18 HRRR/GFS ingest jobs still pinned to hand-tagged images, not in CI (pre-existing, unchanged).
- `endRace` best-effort pattern likely exists elsewhere (audit other fire-and-forget API calls on mobile).
- Garmin action rendering is firmware-dependent; if the Fenix test fails, a Connect IQ companion app becomes a real (larger) work item.

## 6. Open items
- Decide sanity distance for the "indefinite" start line (proposal: 1 nm each side of the mark).
- Gate-ray direction derivation (from inbound/outbound leg geometry + rounding side) needs a short design note before 2c.
