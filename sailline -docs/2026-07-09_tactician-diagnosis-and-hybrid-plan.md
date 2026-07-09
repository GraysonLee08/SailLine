# AI Tactician — Diagnosis + Hybrid Guidance Plan (2026-07-09)

Grill session outcome. Problem reported: no tactician calls ever seen,
on-water (Beer Can 7.1) or driving sims. Repo review + interview
resolved both the bug and a scope evolution. No code written this
session — plan awaiting approval per project rules.

## Root-cause findings (repo evidence)

1. **Tier gate has discarded every evaluation.** `telemetry.py:465`
   spawns the pipeline only for `tier in ("pro","hardware")`;
   `user_profiles.tier` defaults to `'free'` (migration 0001) and no
   code path ever upgrades it — there is no billing flow. Unless
   Grayson's row was manually set (unconfirmed), the tactician task
   has **never once run** for his account. Primary suspect.
2. **`ANTHROPIC_API_KEY` on the sailline-api service was never
   verified** — open deploy item from 2026-06-11. The recap works via
   the postprocess JOB (separate env). If missing, `advisor.generate_call`
   returns None on every call: permanent silence even after (1).
3. **Ten sequential silent exit points, zero observability.** Global
   cooldown → live-race check → opt-out → ≥3 fresh track points →
   forecast load → detector fire → per-type cooldown → SILENT →
   staleness drop. Only Cloud Run log lines exist; nothing is
   inspectable per-race.
4. Driving/walking sims can never exercise detectors (polar/geometry
   mismatch) — confirmed with Grayson; race setup cannot force them.

## Product decisions (interview 2026-07-09)

- **UX model: hybrid.** Keep event-gated AI calls (quiet-cockpit spec
  stands) AND add a **persistent next-step strip** on the recording
  screen — the Google-Maps feel comes from the strip, not from more
  AI calls.
- **Strip is client-side pure geometry.** Route GeoJSON is already on
  the phone (RoutingContext). Every GPS tick: position along route →
  next heading change >~25° → classify tack/gybe/bear-away from wind →
  "Tack → ~215° in 4 min · 0.6 nm" → then next mark. Degrades to
  "Next mark: SA7 · 1.2 nm" with no route. No backend, no Claude cost.
- **Call delivery: haptic + card + TTS voice** (expo-speech, per-race
  toggle). ≤140-char calls are already speech-shaped.
- **Snapshot inputs:** add currents (sample the routing current grid at
  the boat) and sustained pitch (rolling median, dock-zeroed) as
  **context only** — no new detectors. Pitch detector explicitly
  rejected this phase: no published per-class pitch bands; invented
  thresholds burn trust. PROMPT_VERSION → 3 when these land.
- **Heel:** pre-validate against recorded IMU via the replay harness,
  then one on-water sanity race → flip `HEEL_CALLS_ENABLED`.
- **Tier:** set Grayson's uid to pro now (SQL); keep the gate; add a
  minimal tier-admin path later (phase D) — not billing.
- AIS competitor calls (v1b) remain out of scope.

## Phasing (approved order: A → B → C → D)

**A. Unblock + see (backend, ~1 session)**
- Cloud SQL: `UPDATE user_profiles SET tier='pro' WHERE id='<uid>'`.
- Verify/set `ANTHROPIC_API_KEY` on sailline-api (PowerShell):
  `gcloud run services describe sailline-api --region=us-central1 --format="value(spec.template.spec.containers[0].env)"`
- Evaluation trace: every `_evaluate` run appends a compact record
  (exit gate OR detector near-misses w/ numbers, SILENT, dropped-late)
  to a Redis ring buffer `tactics:trace:{race_id}` (TTL ~24 h).
- `GET /api/races/{id}/tactics/debug` returns the ring buffer
  (owner-only). One structured log line per evaluation with exit reason.

**B. Replay harness (~1 session)**
- CLI + pytest fixtures: replay a recorded race (Beer Can 7.1 track is
  in the DB) through the pipeline batch-by-batch with historical
  forecast; mocked Claude by default, real behind `SAILLINE_AI_SMOKE`.
- Prints gate decisions, near-misses, would-be calls → threshold tuning
  at the desk. Heel pre-validation against recorded imu_samples.

**C. Cockpit UX (mobile, ~1 session)**
- NextStepStrip component (recording screen, top): client-side route
  geometry per above; large type, glove-friendly.
- TTS voice for tactician calls (expo-speech), toggle next to the
  tactician switch; haptic + card unchanged.

**D. Richer snapshot + ops (~1 session)**
- Currents + sustained pitch in snapshot; PROMPT_VERSION 3 + golden
  tests. Heel validation race → flip flag. Minimal tier-admin
  mechanism (script or guarded endpoint).

## Tech debt (existing, reaffirmed)

- Pipeline orchestration still has no mocked-I/O test (flagged
  2026-06-11) — phase B's harness should close this.
- Per-race client toggle isn't checked server-side (bounded waste).
- Heel-band tables Beneteau-only + GENERIC.

## Open items

- Confirm Grayson's uid + whether his row was ever set to pro.
- Announce-window / cooldown starting numbers reviewed after first
  replay-harness run, not before.
