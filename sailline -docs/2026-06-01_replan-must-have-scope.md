# 2026-06-01 — Re-plan: what must work, what's drifting, what to shelve

**Trigger:** 2026-05-31 on-water test ("Harmony Delivery") failed the same way 2026-05-30 ("Colors (Bravo)") failed, plus routing broke. Direction feels wrong.

## What we know from today's data

Race `a1a299b2-7a8f-4f03-8baa-e662e2507dce`, distance mode, 2 marks (Waukegan offshore → Chicago offshore), start 15:10Z.

| Signal | Value | Interpretation |
|---|---|---|
| `point_count` | 2,090 | Recorder uploaded points. GPS path works. |
| `started_at` | 15:10Z | Backend stamped on first telemetry. Race row binding works. |
| `mark_passes` | `[]` | Detector did not register either mark. |
| `ended_at` | `""` | No auto-stop. Session was left open. |
| `course_len` / `pass_count` | 0 / 0 | Downstream math never ran (no passes). |
| `ai_summary` / `wind_snapshot` / `heel_summary` | `""` | Post-process job never fired (gated on final-mark pass). |
| Pre-race route ETA | `—` (screenshot) | `/api/routing/compute` failed or returned a non-displayed state. |

## Root cause (updated with 2026-06-01 Cloud Logging dump)

Cloud Run `sailline-api` logs for the race window, filtered to race id, reveal **the recorder upload pipeline went dark** — not the detector.

Timeline of `POST /api/races/{race_id}/telemetry`:

| Time (UTC) | Status | Notes |
|---|---|---|
| 15:11:12 | 200 | First batch after start |
| 15:13:29 | 200 | Normal |
| — | — | **20-minute gap, recorder offline** |
| 15:33:09 | **500** (8.24 s) | Reconnect dump → backend choked |
| 15:33:18–15:33:29 | 200 ×8 (1/sec) | Buffered backlog drained |
| 15:33:38, 15:34:08, 15:35:51 | 200 | Sparse |
| 15:43:06–15:43:10 | 200 ×4 | Another rapid burst (buffered dump) |
| 15:44:41 | 200 | **Last live batch** |
| — | — | **2h 47m of silence** |
| 18:32:16 | 200 (9.84 s) | Single delayed dump, race already over |

**Conclusions:**

1. **The recorder stopped reliably uploading at 15:44:41Z, ~35 min after start.** Mark 1 is ~1.04 nm offshore (per screenshot at the moment the user noticed); at sailing speeds the boat probably didn't reach CPA on Mark 1 until after uploads went dark. **No data, no detection, no `ended_at`, no postprocess.** Empty `mark_passes` and `ai_summary` are downstream symptoms of the upload failure, not detector bugs.
2. **The 500 at 15:33:09 was the backend choking on a 20-min buffered batch dump.** Recorder was already offline for 20 min before this; the reconnect tried to send everything at once.
3. **No `/api/routing/compute` calls appear in the log**, but the filter `textPayload:"a1a299b2"` won't catch them — `race_id` lives in the POST body, not URL or text. So routing may or may not have been called; need a wider query.
4. **The mark-detection rewrite (`adf2c97`) was never actually exercised on real water yesterday.** Detection couldn't run because most of the race's GPS data never reached the backend.

This reframes the entire priority order below.

## What "a race is shipped" must mean

A race recording counts as working only when ALL of these are true end-to-end on one device, one race, no manual intervention:

1. User creates a race (mobile or web), boat class set, marks placed.
2. Pre-race route renders with an ETA and a polyline. If the forecast cycle isn't out, the UI says so in plain language — not a silent `—`.
3. Recorder auto-starts at `start_at` ± 60 s. `started_at` lands in DB.
4. GPS streams to backend in batches throughout the race. Boat dot tracks live.
5. Every mark the boat passes within the configured threshold is detected and appears in `mark_passes` within ~30 s.
6. Final-mark pass triggers auto-stop. `ended_at` is set. Postprocess fires.
7. Stats screen renders the AI summary, leg splits, heel summary.
8. If automation fails at any step, a manual Stop button is reachable and ends the session cleanly with whatever data is on hand.

Anything we ship between now and proof of (1)–(8) on a real race is a distraction.

## What we delete or shelve until (1)–(8) hold

These are not bad ideas. They just don't earn their keep until the core loop works.

- **`MarkPassControls` polling hook** — `useMarkPasses` polls the backend during recording. Useful UI, but right now it's polling for data that won't ever populate because detection isn't proven on real water. Keep the component shell; pause the polling and the missed-mark notifier until we have one real race with passes.
- **`useMissedMarkNotifier`** — same. Notifies on a state we haven't yet observed. Shelve.
- **Recording UI behind `VITE_RECORDING_ENABLED=false` on web, with hooks still arming** — already flagged as tech debt 2026-05-30. Either gate the hook arming too OR keep Stop visible. No middle ground.
- **Theme provider / dark mode** — ship after core loop. Visible effort, zero impact on whether a race records.
- **Heel gauge / IMU UI** — shipped 2026-05-28 but `heel_summary` is `""` because the recorder still only persists GPS. Either wire IMU into the upload payload now or hide the gauge until it does — having a live dial that flows nowhere is misleading.
- **Better-route SSE banner** — depends on routing working in the first place. Don't touch until baseline `/compute` is trusted.
- **Auto-route settings, orientation controls** — keep, but no new work here.
- **Any new mobile screen, new FAB, new picker** — none. Code freeze on surface area.

## What we work on, in order

Single-track until each step is proven on water or in a fixture:

### 1. Diagnose 2026-05-31 (no code, just inspection)

- Pull `track_points` for race `a1a299b2`. Plot vs. Mark 1 (42.3561, -87.8180) and Mark 2 (41.8493, -87.5995). Compute minimum approach distance per mark.
- Pull Cloud Logging for `sailline-api` POST `/api/routing/compute` for this user/race. Capture the response shape (200 vs 425 vs 5xx).
- Confirm the Cloud Run revision active at 15:10Z 2026-05-31 contains commit `adf2c97`.
- Write findings here under a new `## 2026-06-01 diagnosis` section. No code yet.

### 2. Fix exactly one thing per session, validate, then move on

Reordered after 2026-06-01 log diagnosis. Each step is its own session, no parallel branches:

A. **Recorder upload reliability.** This is the actual blocker. The mobile recorder must (i) detect when uploads are failing or the network is down, (ii) buffer locally without dropping points, (iii) reconnect and drain the buffer in **bounded batches** the backend can process (not one giant dump that 500s), (iv) surface a visible UI indicator when uploads are stalled so the user isn't lulled by "ON LINE" while data is being lost. Validate by recording a 30-min outing, intentionally toggling airplane mode mid-race, and confirming every point reaches DB and detection fires.

B. **Backend batch-size guard / robustness.** The 500 at 15:33:09 (8.24 s) on a reconnect dump means `/telemetry` can't handle a buffered backlog. Either cap accepted batch size and have the client chunk, or make the handler stream/iterate rather than loading everything. One change, with a regression test using a 1k-point batch.

C. **Mark detection on real water.** Re-test after A and B. Detector was never exercised yesterday because data didn't get there. If passes still miss with reliable data, then fix.

D. **Manual Stop reachable in all states.** Whatever `VITE_RECORDING_ENABLED` becomes, Stop must always be findable. One commit.

E. **Routing pre-race "—" diagnosis.** Re-query Cloud Logging for `POST /api/routing/compute` without the race-id filter and inspect status. If 425, UI needs "forecast available at HH:MM" chip instead of dash. If 5xx, find and fix.

F. **`ended_at` written on final pass or manual stop.** Verify end-to-end.

G. **Postprocess fires and writes `ai_summary`.** Confirm Cloud Run Job triggers.

Anything not in A–G is shelved.

### 3. Definition of done for this re-plan

Three consecutive on-water race recordings, no manual intervention beyond Start, where DB row has: `started_at`, all expected `mark_passes`, `ended_at`, non-empty `ai_summary`. Until that bar is hit, no new features land.

## Tech debt that's actively dangerous

- `useMarkPasses` polling fires regardless of whether passes can be detected. Wastes API calls and creates the appearance of liveness for a dead pipeline.
- Recorder gate on web: hooks arm, UI hides. User can be in a "recording" state with no UI to escape. Flagged 2026-05-30, still standing.
- IMU samples shown in UI, never uploaded. `heel_summary` will always be empty even on a successful race.
- `EngineVersion` is part of route cache key but I haven't verified it bumped with the recent routing-adjacent changes. Stale cache could be why ETA shows `—`.

## Decisions

- **Keep commit `adf2c97`.** The detector rewrite is the right direction; the test against the Bravo GPX shows 7/7 detection in fixture. Don't revert. Fix forward.
- **Stop adding code until diagnosis is complete.** Today (2026-06-01) is inspection only.
- **Single-track work.** No more parallel feature branches alongside an unproven core loop.

## Next session

Run the three diagnosis tasks in §1, log findings here, then pick A from §2. Do not start B until A is proven on water.
