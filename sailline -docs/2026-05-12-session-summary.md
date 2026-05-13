# Session Summary — 2026-05-12

## What we built

**Step 1 — Server-side Kalman filter** (`backend/app/services/attitude.py`)
- Two-channel 2-state Kalman (heel + pitch independently), each tracking `[angle, gyro_bias]`
- Tilt-compensated pitch formula (`atan2(-ay, sqrt(ax² + az²))`) for accuracy at non-zero heel
- 13 unit tests covering static tilt, sinusoidal roll, gyro bias rejection, step response, initialization, robustness to bad timestamps — **all passing in 0.07 s**

**Step 2 — Client-side IMU sensor module** (`frontend/src/sensors/imu.js`, `frontend/src/SensorDebugView.jsx`, `frontend/src/AppView.jsx` patched)
- Cross-platform: iOS `DeviceMotionEvent` + `requestPermission()`, Android Generic Sensor API
- 10 Hz sampler with latest-value downsampling
- Tiny client-side complementary filter for the gauge display
- Hidden debug page reachable only via `?debug=sensors` — no UI entry point
- **Verified on real Android (10 Hz exact, gravity in correct axis, gyro zeros when still) and real iOS**
- Sleep/wake degrades gracefully (~6.5 Hz when backgrounded; production race mode will use wake lock)

**Step 3 — WebSocket telemetry stream** (`backend/app/auth.py` refactored, `backend/app/routers/telemetry_stream.py` new, `backend/app/main.py` patched, `backend/tests/test_telemetry_stream.py` new)
- `WS /api/races/{race_id}/telemetry/stream?token=<firebase>&resume_from_t=<float>`
- Auth via shared `_verify_token_string` helper, callable from HTTP and WS
- 10 Hz raw IMU in → Kalman fusion → 10 Hz `{type, heel_deg, pitch_deg}` out
- 15 s heartbeats, bounded outbound queue with drop-oldest backpressure
- Calibration offsets pulled from existing `race_calibrations` at handshake
- 11 tests passing in 1.12 s

## Key architectural decisions made this session

- **Two-transport architecture (Path 2):** existing REST POST `/api/races/{id}/telemetry` keeps doing durable storage; new WebSocket handles the real-time advisor/gauge path. No duplicate data — REST is source of truth for the historical track; WS writes nothing to the DB.
- **Server-side Kalman, client-side complementary filter** for the gauge (so spirit-level UI doesn't depend on network round-trip).
- **Calibration via the existing `race_calibrations` append-only history table** (better than the per-race columns we initially proposed — supports mid-race re-zeroing with retroactive correction).
- **iOS + Android both supported.** Streams differ at the API level, payload identical.
- **State machine:** `setup-calibrating` (sensors + Kalman on, no advisor) → `pre-race` (auto-start at T-5 min) → `racing` (advisor on).
- **Sign conventions (NMEA 2000):** heel positive starboard, pitch positive bow-up.

## Open items / follow-ups

- **Step 3.5 (Piece B) — Client-side reconnect manager.** Not yet implemented. Will live in the frontend; handles WS close events, exponential backoff, optional `resume_from_t` on reconnect, surfaces "Reconnecting…" to the UI.
- **Step 4-client — Wire the sensor module to the WS.** Currently the debug page only renders locally. Needs a hook (`useTelemetryStream`) that opens the WS, sends raw IMU, consumes attitude messages.
- **Step 5 — Calibration UI.** "Calibrate sensors" button in `RaceEditor.jsx` that captures ~15 s of fused output (long enough to wash out wave motion) and POSTs a row to `race_calibrations`.
- **Wake lock on race start** — `navigator.wakeLock.request('screen')` per the plan; not yet wired.
- **iPhone permission flow on the sensor module** is verified to work, but only on the debug page so far. Will re-validate once tied to the real race-start flow.

## Important caveat learned this session

I designed Steps 3–5 as if the backend were empty, then discovered a substantial existing `telemetry.py` REST endpoint and migration `0004_add_imu_samples` already in the repo. This forced a significant plan revision and burned a chunk of time. **Going forward: search project knowledge before any architecture recommendation.** Added as a permanent memory rule.

## Suggested next session

**Step 3.5 → Step 4-client → Step 5**, in that order. Step 3.5 (reconnect manager) is independent and small, ~3–4 hours. Step 4-client builds on it. Step 5 caps it off with the user-facing calibration button.
