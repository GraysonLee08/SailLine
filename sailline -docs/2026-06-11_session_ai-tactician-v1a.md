# 2026-06-11 Session (2 of 2) — AI Tactician v1a+v1c code + IMU upload

Second session today (first: conus HRRR ingest fix). Started from "where
are we / what's next for commercial," shipped step 2.5 (auto-route on
race select), specced the in-race AI tactician, then built v1a + v1c
end-to-end. Spec (iterated with Grayson through four rounds):
`2026-06-11_ai-tactician-spec.md`.

## What we worked on

1. **Step 2.5 — auto-compute route on race select** (mobile). Quiet
   mode on `useRouting.compute({quiet:true})`; RoutingContext's
   race-change effect clears then auto-computes (skips finished and
   <2-mark races). Sideloaded and confirmed by Grayson.
2. **AI tactician spec** — four decisions locked: SSE+notifications
   channel; all three advice classes (perf coaching, wind/route
   tactics, AIS later); event-triggered; detectors decide WHEN, Claude
   reasons over a snapshot for WHAT. Two design principles added at
   Grayson's insistence: **lead time is the product** (ETA-windowed
   maneuver calls, staleness guard drops late calls) and **coaching
   calls name the adjustment** (pinching, over-heel → specific trim
   action from a per-boat rule table).
3. **Backend tactics module + wiring + migration + tests.**
4. **Mobile: IMU upload (v1a) + tactics SSE/card/notification/toggle.**

## Repo-reality discoveries (docs were stale)

* `telemetry_stream.py` (WS + server-side Kalman `attitude.py`),
  `performance.py` (Target-Actual engine incl. per-fix
  `evaluate_point`), and `heel_stats.py` already existed — built
  2026-05-12 in the web era, missed by the dev plan (reconciled
  2026-05-07). The tactician REUSES `evaluate_point` instead of a new
  wind-inference module; the WS stays parked (a JS WebSocket dies with
  the screen locked, so it can't serve mid-race advice anyway).
* Migrations were at **0020**, not 0015. New table is **0021**.
* Mark radius and better-route auto-accept were already fixed/shipped —
  memory + commercial-gap list corrected.

## Files changed

### Backend — new

- `app/services/tactics/__init__.py` — package doc.
- `app/services/tactics/detectors.py` — 7 pure detectors in two
  classes. Maneuver (ETA-windowed 90–300 s): planned_maneuver (reads
  the active route's heading changes — zero inference), layline
  (opposite-tack best-VMG line through next mark), forecast_shift
  (looks 5/10/15 min ahead in the wind grid). Coaching
  (persistent-condition): pinching/sailing-low (TWA vs best-VMG angle,
  GPS-only), over_heel (sustained heel vs band + mount-quality gate),
  off_pace (speed-ratio catch-all), plan_divergence (XTE > 300 m).
  Priority-sorted runner; `include_heel` flag is the v1c gate.
- `app/services/tactics/heel.py` — rolling 60 s heel median + stdev
  with calibration offsets applied at read; mount-quality gate
  (stdev > 18° ⇒ suppress all heel calls).
- `app/services/tactics/heel_bands.py` — per-class optimal-heel +
  trim-rule tables. Beneteau 36.7 row (18–22° upwind, reef ladder);
  GENERIC fallback matching the recap prompt's guidance.
- `app/services/tactics/snapshot.py` — ~1–2k-token advisor input:
  trigger + other candidates, decimated 5-min track, 2-min perf
  summary, wind now/+5/+10/+15 at the boat, next mark, last 3 calls.
- `app/services/tactics/advisor.py` — Anthropic bridge mirroring
  race_summary (lazy SDK, injectable client, None-on-failure).
  PROMPT_VERSION=1. Hard prompt contract: ≤140 chars, maneuver calls
  MUST state time-to-event, coaching calls MUST name an adjustment
  from the snapshot, SILENT allowed. parse_response truncates at word
  boundary rather than dropping an overlong good call.
- `app/services/tactics/pipeline.py` — orchestration: global cooldown
  (SETNX 180 s) → live-race + app_settings opt-out gates → context
  loads → evals via `performance.evaluate_point` → detectors →
  per-type cooldown (600 s) → snapshot → Claude via
  `asyncio.to_thread` → **post-model staleness re-check (drops late
  maneuver calls, logged)** → INSERT `tactician_calls` + publish
  `{type:"tactics"}` on `route:notifications:{race_id}` + store
  `tactics:latest` for SSE replay. HEEL_CALLS_ENABLED=False (v1c flag).
- `migrations/versions/0021_add_tactician_calls.py` — append-only
  calls table + (session_id, created_at DESC) index. **Manual apply
  before deploy** per runbook.
- `tests/test_tactics.py` — 30 tests: every detector fire/quiet path,
  announce-window edges, mid-tack suppression, mount gate, band
  fallback, runner priority + heel gate, advisor SILENT/truncation/
  fake-client/failure, snapshot shape, key fingerprints.

### Backend — edited

- `app/services/redis_keys.py` — `route_current_key`,
  `tactics_latest_key`, `tactics_cooldown_key`, TTL consts.
- `app/routers/telemetry.py` — Pro-gated fire-and-forget
  `evaluate_tactics_safe` after the batch commits (lazy import).
- `app/routers/routing.py` — stores `route:current:{race_id}` on every
  successful compute (feeds plan-based detectors; non-fatal).
- `app/routers/routing_notifications.py` — SSE publisher routes
  messages by payload `type` → named events `alternative` | `tactics`;
  replays `tactics:latest` on connect alongside the alternative.

### Mobile — new

- `src/recorder/imuRecorder.ts` — 2 Hz boat-frame attitude capture →
  IMU-only `{imu, calibration?}` batches every 30 s. Separate from the
  GPS pipeline ON PURPOSE (native mode owns GPS batches; backend
  accepts IMU-only). RAW upload per backend contract (server applies
  zero-offsets); re-reads orientation AsyncStorage each flush so a
  mid-race Zero uploads within one cycle. Bounded in-memory queue.
- `src/hooks/useTacticianSetting.ts` — per-race toggle (default ON),
  clone of useAutoRouteSetting.
- `src/notifications/tactics.ts` — local notification (always-replace
  per race, informational, race-start channel).
- `src/components/TacticianCard.tsx` — dismissible call card: vibrate
  pattern on new call, auto-expires (4 min maneuver / 2 min coaching),
  44pt dismiss target.

### Mobile — edited

- `src/recorder/useTrackRecorder.ts` — best-effort IMU sidecar:
  start/stop/unmount wiring, ring-buffer log line; GPS path untouched.
- `src/hooks/useRouteNotifications.ts` — `tactics` named event +
  `TacticsPayload` + `dismissTactics`.
- `src/routing/RoutingContext.tsx` — exposes `tactics`/`dismissTactics`
  (plus this morning's auto-compute-on-select effect).
- `app/(app)/recording.tsx` — TacticianCard in the bottom stack,
  backgrounded-app local notification (AppState + created_at dedupe),
  notification cleanup on stop, toggle gating.
- `src/components/RaceDetailSheet.tsx` + `app/(app)/index.tsx` —
  "AI Tactician" toggle row below Auto-route, same pattern.

## Decisions + rationale

- **Reuse `performance.evaluate_point`** as the per-fix engine —
  exists, tested, derives TWA/TWS exactly as routing does.
- **Forecast wind as truth for targets** (matches routing + the
  performance engine); observed-vs-forecast divergence is detector 7's
  job, feeding the existing better-route machinery.
- **IMU as a sidecar, not in GPS batches** — native uploader owns GPS;
  backend accepts IMU-only batches; sensor failure can't touch the track.
- **Staleness guard in code after the model round-trip** — never trust
  the prompt with timeliness; late maneuver calls are dropped + logged.
- **`route:current` Redis key** added rather than recomputing the cache
  key — the compute endpoint stores the Feature the user is looking at.
- **Heel calls code-complete but flag-gated** (`HEEL_CALLS_ENABLED`) —
  flip after one on-water sanity race comparing logged sustained heel
  to crew observation. Pinching ships live (GPS-only).

## Verification

- Sandbox: all 9 new backend files `py_compile` clean (pipeline via
  outputs staging — the E:\ mount served stale bytes for edited files
  again); 4 new mobile files esbuild-parse clean.
- **NOT yet run (Windows required):** `pytest` full suite,
  `npx tsc --noEmit` in `mobile/`, sideload build. The repo's
  node_modules esbuild is fine on Windows — the sandbox-mount copy is
  corrupt, ignore that.

## Deployment steps (in order)

1. Windows: `pytest` in `backend/`, `npx tsc --noEmit` in `mobile/`.
2. Apply migration: `alembic upgrade head` (additive, sub-second).
   Verify DB password secret first (known drift).
3. Push to main (Cloud Build gates on pytest; deploys API).
4. `gradlew assembleRelease` + `adb install -r` for the mobile side.
5. Confirm `ANTHROPIC_API_KEY` is set on the sailline-api service
   (recap already uses it via the postprocess JOB — the API service
   may not have it; the tactician degrades to silent if missing).

## Open items / next steps

- On-water shakedown: confirm calls arrive, tune thresholds from the
  logged `diagnosis` blobs + DROPPED-call log lines.
- Heel validation race → flip `HEEL_CALLS_ENABLED`.
- Review screen: "calls made" timeline from `tactician_calls` (table
  is ready; UI not built).
- Recap prompt: feed tactician calls into the post-race debrief
  (PROMPT_VERSION bump when done).
- v1b: AIS competitor detector.
- Settings screen: global tactician opt-out UI (server already reads
  `app_settings.tactician.enabled`).

## Technical debt flagged

- **Pipeline orchestration has no mocked-I/O test** — pure layer is
  covered (30 tests); the Redis/DB/publish dance is deploy-verified
  only. Write `test_tactics_pipeline.py` with mocked pool+redis next
  backend session.
- IMU queue is in-memory only — a crash loses ≤30 s of attitude (GPS
  unaffected). Acceptable; revisit if heel analytics get load-bearing.
- `_event_name_for` JSON-parses every SSE message — fine at these
  volumes, wasteful pattern if the channel ever gets chatty.
- Tactician evaluates per telemetry batch for every Pro user even
  with the client toggle off (server checks only the global
  app_settings opt-out). Wasted Claude calls are bounded by cooldowns;
  fold the per-race setting into app_settings later.
- Heel-band + trim tables are Beneteau-only + GENERIC — same sourcing
  task as the polar gap; do them together.
- **Development plan.docx not updated this session** — the sandbox
  mount served stale bytes for edited files today and a python-docx
  round-trip through a stale read risks destroying the uncommitted
  edits already in the file. Update it from Windows or next session.
