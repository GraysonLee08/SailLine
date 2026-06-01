# 2026-06-01 — Durable recorder upload pipeline: design

**Status:** PLAN ONLY. No code changes proposed before review.
**Related:** `2026-06-01_replan-must-have-scope.md` (priority A in §2 of that doc).
**Trigger:** 2026-05-31 race recorded 2,090 GPS points but the backend received them in bursts with a final 2h 47m silence; mark detection had no data to act on.

---

## 1. What I read

To make this plan grounded, not guessed, I read:

- `mobile/src/recorder/useTrackRecorder.ts` — flush loop, queue handling, error surface
- `mobile/src/recorder/queue.ts` — AsyncStorage durability
- `mobile/src/recorder/backgroundGeolocation.ts` — Transistorsoft v5 config
- `mobile/src/recorder/RecorderContext.tsx` — provider/lifetime
- `mobile/src/api.ts` — Firebase-tokened fetch wrapper, no retry, no timeout
- `mobile/app/_layout.tsx` — module-scope registrations, headless-BG-fetch already a known concept
- `mobile/app/(app)/recording.tsx` — what the user sees during a race
- `mobile/src/components/GuidanceCard.tsx` — source of the "ON LINE" label
- `backend/app/routers/telemetry.py` — batch caps (100 GPS / 1000 IMU), single transaction, non-idempotent
- `backend/app/services/track_ingest.py` — detection-and-UPDATE path, transactional with the INSERT
- Cloud Logging for race `a1a299b2` 2026-05-31 14:30Z–16:30Z

---

## 2. What's actually wrong (six distinct issues)

### 2.1 "ON LINE" is not a connectivity indicator

`GuidanceCard.tsx:69` sets the "ON LINE" label when `|crossTrackM| < 15`. This is a **navigation** signal: "your heading is on the rhumb line to the next mark." It says nothing about whether GPS points are reaching the backend. The user saw "ON LINE" and reasonably assumed tracking was working; in reality, uploads had been silent for many minutes.

**There is no UI surface today that shows upload health.** `recorder.error` renders if a flush throws, and `queueLength` exists in the hook's return but is never rendered on the recording screen.

### 2.2 JS-driven flush timer cannot be trusted when the screen is locked

The recorder relies on `setInterval(flushNow, 30_000)` in JS. When the phone screen locks and the app is backgrounded, the React Native JS bridge sleeps. Transistorsoft's native foreground service keeps GPS **capture** alive (this is the whole reason we adopted it), but JS timers do not fire reliably in the background. `onPosition` callbacks may queue and replay when JS wakes, but a steady 30 s flush cadence is not delivered.

This explains the log pattern: bursts of POSTs separated by long silences match the user opening their phone, JS waking, draining the queue, then sleeping again.

### 2.3 The design note explicitly avoided Transistorsoft's native HTTP layer

`backgroundGeolocation.ts:10–14` documents the decision: "Transistorsoft ships its own SQLite persistence + HTTP auto-POST. We do NOT use it." Two stated reasons:

1. The plugin posts its own location schema, not our `{gps:[...]}` shape.
2. Threading hourly-expiring Firebase tokens through the native HTTP layer is fragile.

Both reasons are real but solvable. (1) is fixed by Transistorsoft v5's `httpRootProperty` / `locationTemplate` / `params` config — the plugin can emit our exact wire shape. (2) is solvable via `BackgroundGeolocation.setConfig({headers})` refreshed on a foreground tick, or per-request header injection. The current "JS owns uploads" choice is what fails in the background; the native uploader exists precisely for this case.

### 2.4 The cold-start 500 at 15:33:09 has no retry path

The 500 took 8.24 s, which strongly suggests a Cloud Run cold start (instance scaled to zero after 20 min of no traffic, then handler ran before the asyncpg pool was warm or the DB connector was initialized). The recorder keeps the queue on non-200, but does not retry — it waits for the next interval or the next 100-point trigger. With reliable backoff and retry, a cold-start 500 is a non-event; without it, the 500 sat in the user's log staring back at them.

Also: `apiFetch` sets **no fetch timeout**. A request that hangs forever blocks all subsequent flushes via `flushingRef.current`.

### 2.5 Backend is not idempotent

`telemetry.py:24–30` calls this out: a re-sent batch inserts duplicate `track_points` rows. The current design depends on the client only retrying when it knows the server didn't process the batch (non-200). But "I didn't get a 200" can mean either "server got the request and committed but the response was lost" or "server never saw it." The client must retry either way, which means the backend MUST be idempotent or we will create duplicates in the durable-queue model.

### 2.6 Race-row reads are not concurrency-safe

`detect_and_persist_new_passes` reads `existing_passes` from the row, runs the detector starting at `len(existing_passes)`, and UPDATEs with the new full list. Two concurrent batches (e.g. a delayed offline batch + a fresh online batch) could both read the same `existing_passes`, both compute new passes, and the second UPDATE overwrites the first. This wasn't the cause of 2026-05-31's failure (uploads were serial), but the durable-queue design we're moving to makes overlap more likely. Worth fixing now, while we're in the area.

---

## 3. Design principles

Before listing changes, the principles I want this design to obey:

1. **Native owns capture AND upload.** JS owns UI and decisions. We've already accepted native capture (Transistorsoft foreground service). Extending the same boundary to upload eliminates the JS-sleep problem entirely. JS becomes the supervisor: configure, observe, retry on demand, surface status.
2. **Durable queue is the source of truth.** A point is "captured" the moment it's in the queue. It's "uploaded" the moment the server returns 200. No intermediate state.
3. **Backend is idempotent.** The client retries the same batch identifier as many times as it needs to. The server inserts the rows exactly once. This makes failure modes simple: retry until 200.
4. **Bounded batches at the boundary.** Backend caps batches at 100 GPS / 1000 IMU. Client chunks reliably to that cap. A reconnect dump cannot be one giant request.
5. **Backoff and circuit breaker.** Failed requests retry with exponential backoff. Repeated failures pause uploads briefly so we don't spin against a dead network.
6. **Honest status.** The UI shows "Live," "Buffering," "Stalled," or "Offline" based on actual upload state — not navigation state. The user must be able to tell at a glance that data is or isn't flowing.
7. **Single-track ship.** Every change in this plan is gated by a test or an on-water validation. No new surface area until proven.

---

## 4. Architecture (target end state)

```
        ┌────────────────────────────────────────────────────┐
        │ Native layer (Transistorsoft v5 foreground service)│
        │                                                    │
        │   • 1 Hz GPS capture (already in place)            │
        │   • SQLite-backed local queue (built in)           │
        │   • HTTP auto-POST (NEW: enable + customize)       │
        │     - POST to /api/races/{raceId}/telemetry        │
        │     - Body shaped via locationTemplate /           │
        │       httpRootProperty to match our wire schema    │
        │     - Authorization header refreshed by JS on      │
        │       foreground / token-changed                   │
        │     - Retries with exponential backoff (built in)  │
        │     - Bounded batch size = 100 (built in)          │
        └────────────────────────┬───────────────────────────┘
                                 │ status events
        ┌────────────────────────▼───────────────────────────┐
        │ JS layer (RecorderProvider + useTrackRecorder)     │
        │                                                    │
        │   • Subscribes to native HTTP success/failure      │
        │   • Maintains derived state: Live / Buffering /    │
        │     Stalled / Offline                              │
        │   • Owns auth-token refresh into the native layer  │
        │   • Owns the manual "flush now" and "stop" actions │
        │   • Renders the connectivity badge                 │
        └────────────────────────────────────────────────────┘
```

JS no longer drives the timer-based flush. The native layer pulls points from its own queue, posts in 100-sample chunks, retries with backoff, and emits events. JS observes and decides.

---

## 5. Backend changes (idempotency + concurrency)

These ship FIRST because the mobile changes depend on them.

### 5.1 Idempotent ingest (`/telemetry`)

- Add a per-`(session_id, recorded_at)` unique constraint to `track_points` via Alembic migration: `ALTER TABLE track_points ADD CONSTRAINT track_points_session_recorded_uniq UNIQUE (session_id, recorded_at)`.
- Change the bulk insert in `telemetry.py` to `INSERT … ON CONFLICT (session_id, recorded_at) DO NOTHING`.
- Same treatment for `imu_samples` keyed on `(session_id, recorded_at)`.
- Update the docstring at `telemetry.py:24–30` to reflect the new contract.
- Tests: replay the same 100-point batch twice, assert exactly 100 rows land and the second response returns `gps_inserted = 0` (server reports what it actually inserted, not what the client sent).

### 5.2 Concurrency-safe pass detection

Wrap the detection-and-UPDATE in an explicit row lock so two concurrent batches serialize:

```sql
SELECT marks, mark_passes, started_at, start_at, mode
FROM race_sessions
WHERE id = $1
FOR UPDATE
```

This already happens inside a transaction; adding `FOR UPDATE` makes the read-modify-write atomic. Cost is negligible; we never have more than a handful of concurrent batches per race.

### 5.3 Bounded-batch sanity (already in place — keep, don't regress)

`MAX_GPS_SAMPLES_PER_BATCH = 100` returns 413. Keep this. The native uploader respects it.

### 5.4 Cold-start mitigation

This is the cheapest 80% fix for the 15:33:09 500: set `min-instances=1` on the Cloud Run service so the first request after idle doesn't pay startup cost. Cost is about $5/mo at idle and removes the entire cold-start failure mode. Worth the spend for race-day reliability.

---

## 6. Mobile changes (native uploader + status)

### 6.1 Switch to Transistorsoft's HTTP layer

In `backgroundGeolocation.ts`, extend the `ready()` config with an `http` block:

```ts
http: {
  url: `${API_URL}/api/races/{raceId}/telemetry`,
  method: "POST",
  autoSync: true,
  autoSyncThreshold: 1,          // start posting as soon as one point queued
  batchSync: true,
  maxBatchSize: 100,             // matches backend cap
  headers: { Authorization: "" },// filled and refreshed by JS
  httpRootProperty: "gps",       // wraps as { gps: [...] }
  locationTemplate: `{
    "t": "<%= timestamp %>",
    "lat": <%= latitude %>,
    "lon": <%= longitude %>,
    "sog_kts": <%= speed * 1.943844 %>,
    "cog_deg": <%= heading %>,
    "gps_acc_m": <%= accuracy %>
  }`,
}
```

Notes:

- `raceId` must be templated per-race. Transistorsoft's `setConfig({ url })` is called when the user picks a race; `clearConfig` or rebind on stop.
- The wire shape must match `TelemetryBatch` in `telemetry.py` exactly. The above template needs verification once we test against a deployed endpoint — values like `speed` may be `-1` when unavailable; the template must emit `null` not a negative.
- IMU samples have to flow through the same HTTP layer or a separate JS path. Phase 1 ships GPS-only via native; IMU stays JS-flushed (or shelved) until the native path is proven. Today's race had `imu` empty regardless.

### 6.2 Token refresh into native config

Firebase ID tokens expire hourly. A long race outlives one token. Add:

- A foreground listener (`AppState.addEventListener("change", ...)`) that refreshes the token when the app comes to foreground and pushes it into Transistorsoft via `setConfig({ headers: { Authorization: "Bearer …" } })`.
- A periodic JS refresh while the app is foreground (every 30 min) using `setInterval` — fine here because we only need it to work while JS is awake, and JS is awake when the app is foreground.
- A `Firebase.onIdTokenChanged` listener as the third source so token rotation always reaches the native layer eventually.

When JS is fully asleep and the token expires, the native layer's POSTs will fail with 401. Transistorsoft's backoff retries them. The next time JS wakes (foreground, BackgroundFetch wake, or notification tap), it refreshes and the backlog drains. This is acceptable because we're not losing capture, only delaying upload.

### 6.3 Status state machine

Add a `UploadStatus` enum to `useTrackRecorder` derived from native HTTP events:

| Status | Condition | UI |
|---|---|---|
| `Live` | last successful POST < 60 s ago AND queue depth < 10 | green dot |
| `Buffering` | queue depth ≥ 10 AND last POST < 5 min ago | yellow dot + count |
| `Stalled` | last successful POST ≥ 5 min ago AND queue depth > 0 | orange "Stalled — N pts" |
| `Offline` | last POST attempt failed AND NetInfo reports no network | grey "Offline — N pts" |

Source the events from:

- `BackgroundGeolocation.onHttp` — fired on every POST attempt with `status` and `success`.
- `BackgroundGeolocation.getCount()` — current queue depth, polled when status badge mounts and every 30 s while foreground.
- `@react-native-community/netinfo` — distinguishes "offline" from "online but failing."

### 6.4 Visible badge on recording screen

Add a small badge above or beside the existing "LIVE" pill at the top of `app/(app)/recording.tsx`. Tappable to open a details sheet showing:

- Current status (Live / Buffering / Stalled / Offline)
- Queue depth
- Time since last successful upload
- A "Force upload now" button (calls `BackgroundGeolocation.sync()`)
- A "Stop recording" button that always works regardless of status

### 6.5 Stop is always reachable

Per `recording.tsx`, Stop is already on the recording screen. Keep that. Add: Stop button stays visible and tappable even if `recorder.error` is non-null, the network is dead, or the buffer is huge. Stop must commit `ended_at` locally (queue an `end_session` POST) and let the native layer drain it whenever it next can.

### 6.6 Remove the JS flush timer

After the native uploader is proven, delete `setInterval(flushNow, 30_000)`. Keep `flushNow` as a manual user action (the "Force upload now" button). The native layer's `autoSync: true` replaces the timer's job.

---

## 7. Telemetry on the recorder itself

We learned about the 2026-05-31 failure from Cloud Logging. We need to learn about it from the recorder too, so we don't depend on the user uploading log dumps.

- Locally log every HTTP attempt outcome to a ring buffer (last 200 entries) persisted to AsyncStorage. Surface from a debug screen.
- On stop, the recorder POSTs a summary blob to a new `/api/races/{id}/recorder-debrief` endpoint: total points captured, total uploaded, max queue depth seen, count of 5xx/4xx/network errors, longest gap between successful uploads. Stored on `race_sessions` for the post-race UI.

Cost is small; the value is being able to diagnose the next failure in 30 seconds instead of 30 minutes.

---

## 8. What we shelve (re-stating from the re-plan doc, scoped to recorder)

- `useMarkPasses` polling — pause until durable upload is proven. Polling for data that won't arrive wastes API calls and creates the illusion of liveness.
- `useMissedMarkNotifier` — same.
- IMU upload — keep capture (cheap, captured-but-unused is fine), defer the upload path until GPS upload is solid. `heel_summary` stays empty; that's an honest empty, not a broken one.
- Heel gauge UI — hide until IMU upload ships; misleading otherwise.
- Better-route SSE banner — depends on routing working. Out of scope for this plan.

---

## 9. Test strategy

Each step ships with a test or an on-water validation. No commit lands without one.

### 9.1 Backend tests (pytest)

- `test_telemetry_idempotent_duplicate_batch` — POST the same 100-point batch twice, assert 100 rows in DB and second response reports `gps_inserted=0`.
- `test_telemetry_concurrent_batches_serialize` — fire two overlapping batches against the same race, assert mark_passes contains every pass exactly once.
- `test_telemetry_413_on_oversize_batch` — POST 101 GPS samples, expect 413.

### 9.2 Mobile unit tests (vitest in `packages/shared` where possible; type-check otherwise)

- `gpsPointToWire` round-trip with the new Transistorsoft template values (null SOG, negative accuracy, missing heading).
- `UploadStatus` reducer transitions for each (event, queue, time) combination.

### 9.3 Mobile integration test (manual but scripted)

A documented manual procedure in `sailline -docs/recorder-test-procedure.md`:

1. Start a recording, leave the phone face-down with screen locked for 30 minutes.
2. Toggle airplane mode for 5 minutes at the 10-minute mark.
3. Stop recording.
4. Verify: DB row count == captured count; gaps between adjacent `recorded_at` values ≤ 2 s except across the airplane-mode window; `ended_at` is set; no orphaned recorder-debrief entries.

Until this test passes once, the durable pipeline is not shipped.

### 9.4 On-water validation

One full race recording, no manual intervention beyond Start. Pass criteria from the re-plan doc §3.

---

## 10. Phased ship plan

Each phase is its own commit and PR. Nothing lands without the prior phase proven.

### Phase 1 — Backend idempotency (1 session)

- Migration: unique index on `(session_id, recorded_at)`.
- `INSERT … ON CONFLICT DO NOTHING` in `telemetry.py`.
- `FOR UPDATE` row lock in `track_ingest.py`.
- Tests 9.1.
- Set `min-instances=1` on Cloud Run.

**Validation:** existing on-water-replay test in `test_mark_rounding.py` still passes; new duplicate-batch test passes.

### Phase 2 — Recorder telemetry (1 session, independent of native uploader)

- Add ring-buffer logger to `useTrackRecorder` for every HTTP attempt.
- Add `/api/races/{id}/recorder-debrief` endpoint.
- Surface a debug screen accessible from settings.

**Validation:** record a 5-minute session with intentional WiFi toggle; verify the ring buffer captures the failures and the debrief lands.

### Phase 3 — Status state machine + UI badge (1 session)

- Implement `UploadStatus` derivation from existing events (BEFORE switching to the native uploader; this way the JS-flush regression baseline is visible).
- Add badge to recording screen.

**Validation:** simulate offline by toggling airplane mode; badge correctly transitions Live → Buffering → Offline.

### Phase 4 — Native uploader (the big change; 1–2 sessions)

- Enable Transistorsoft `http` config with template + headers.
- Token refresh hookup (foreground, periodic, onIdTokenChanged).
- Remove JS `setInterval(flushNow)`; keep `flushNow` as manual.
- Rewire UploadStatus to subscribe to `onHttp` and `getCount`.

**Validation:** manual integration test 9.3 passes. Then one on-water race.

### Phase 5 — Cleanup and shelving (1 session)

- Pause `useMarkPasses` polling and `useMissedMarkNotifier` behind a flag until detection is re-validated.
- Hide heel gauge.
- Update `2026-06-01_replan-must-have-scope.md` priority order with results.

---

## 11. Risks and how we'll know we hit them

- **Transistorsoft's `locationTemplate` can't express what we need.** Mitigation: Phase 4 is gated on a small spike that posts a single sample with the template and inspects the body. If the template is too restrictive we fall back to native Headless JS that pulls the SQLite queue and posts via fetch.
- **Token refresh via `setConfig({headers})` doesn't propagate to in-flight retries.** Mitigation: same spike. If retries use stale headers, we wrap the native push behind a JS-side relay that does the actual fetch.
- **The cold-start 500 isn't actually fixed by `min-instances=1`** (something else is slow). Mitigation: Phase 4's manual integration test exposes any residual slow first request; we add a startup-warmup ping if needed.
- **Idempotency migration breaks the legacy `/track` endpoint.** Mitigation: the migration adds a constraint, not a column. Existing inserts that don't conflict are unaffected; existing inserts that DO conflict were already buggy. The legacy endpoint gets the same `ON CONFLICT` treatment in the same commit.

---

## 12. Definition of done

Three consecutive on-water race recordings with all of:

- All captured GPS points land in `track_points` (gaps only across actual GPS dropouts, not upload dropouts).
- Every mark the boat passes within threshold appears in `mark_passes`.
- `ended_at` is set, either by auto-stop or manual Stop.
- `ai_summary` is populated by the post-process job.
- Recorder-debrief shows no `> 5 min` gap between successful uploads.

Then, and only then, we revive `useMarkPasses` polling, `useMissedMarkNotifier`, IMU upload, and the heel gauge.

---

## 13. What I want feedback on before any code lands

1. **Is the native-uploader switch acceptable?** It contradicts the 2026-05-25 design note. I think the note's reasons no longer outweigh the cost we're paying. If you disagree, the alternative is Headless JS + BackgroundFetch wakes, which is more code for the same result.
2. **Min-instances=1 on Cloud Run.** Roughly $5/mo to remove the cold-start hazard. OK to spend?
3. **Idempotency migration ordering.** Need a deploy window where the constraint is added AFTER any existing duplicate rows are deduped. Are there known duplicate rows in `track_points` today? Quick `SELECT session_id, recorded_at, COUNT(*) FROM track_points GROUP BY 1,2 HAVING COUNT(*)>1 LIMIT 10;` will tell us.
4. **Phase ordering.** Phases 1 and 2 are independent and could ship in parallel sessions. Phases 3 and 4 are sequential. Phase 5 is post-success cleanup. OK?

No code until you've reviewed and signed off on the shape.
