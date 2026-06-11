# In-Race AI Tactician — v1 Spec (2026-06-11)

Phase 3's headline Pro feature: plain-language tactical calls delivered to
the phone during a race. This spec reflects the four scoping decisions made
2026-06-11 and the repo as it exists today.

## Decisions locked

1. **Channel:** extend the existing SSE + expo-notifications path. No
   WebSocket in v1 — the advice classes below tolerate seconds of latency.
2. **Scope:** performance coaching + wind/route tactics in v1a; AIS
   competitor awareness in v1b (needs a new analysis layer).
3. **Triggers:** event-triggered only. Deterministic detectors gate when
   the tactician speaks; quiet cockpit by default.
4. **AI role:** detectors decide *when*, Claude reasons over a full
   tactical snapshot to decide *what to say* (or to stay silent).
   Rationale: event-gating keeps cost at pennies per race (10–20 calls ×
   ~2k tokens ≈ <5¢ Haiku / ~15¢ Sonnet), while the snapshot gives Claude
   real context instead of a fill-in-the-blank template.

## Architecture

```
phone (1 Hz GPS, batched ~10s)
  └─ POST /api/races/{id}/telemetry          (existing)
       └─ track_ingest: insert + mark-pass detect   (existing)
       └─ NEW tactics_evaluate(race_id, new_points)
            ├─ load cached race context (route, polar, wind grid, marks)
            ├─ run detectors (pure functions, numpy-free, ~ms)
            └─ detector fired?
                 └─ NEW tactician call (async, fire-and-forget)
                      ├─ build snapshot (JSON)
                      ├─ Claude (Haiku, system prompt vN)
                      └─ publish to Redis pub/sub
                           route:notifications:{race_id}   (existing channel)
  phone ◄─ SSE /api/routing/notifications/{race_id}        (existing)
       └─ NEW message type "tactics" → local notification + on-screen card
```

Key reuse: the SSE endpoint, `useRouteNotifications`, the notification
category plumbing from missed-mark, the `race_summary.py` Anthropic client
+ PROMPT_VERSION pattern, `WindForecast`, polar service, and the live
route already in Redis.

**Latency budget:** telemetry batch cadence (~10s) + detector (~ms) +
Claude (~1–2s) + SSE push ≈ **10–20s advice latency**. Acceptable for
"persistent off-pace" and "shift building" calls; NOT for boat-handling
calls ("ease now") — those stay out of scope until a WebSocket phase.

## Design principle: lead time is the product (2026-06-11)

A call that arrives after the maneuver should have been made is worse
than no call — it costs trust. Every call therefore carries an
**event ETA** and a **minimum lead time** (crew reaction budget,
default ~90s, configurable per call type), enforced in two places:

- **Detectors fire on projected ETA**, not on arrival: they trigger
  when `eta - now` enters the announce window (e.g. 2–4 min out),
  giving the crew time to set up.
- **Staleness guard at publish:** if `eta - now < min_lead` by the time
  the call would go out (slow batch, slow Claude), the call is DROPPED
  and logged — never delivered late. Logged drops feed threshold tuning.

This splits calls into two classes. **Maneuver calls** (tack/gybe/
layline/shift response) are predictive by construction — they project
forward from the forecast grid and the active route plan, both of which
are known ahead of time; timeliness rule = ETA + staleness guard.
**Coaching calls** (pinching, over-heel, trim, pace) are corrections to
a condition that is true now; timeliness rule = persistence check at
publish (dropped if the helm already corrected), capped at one call +
one reminder per episode.

## Detectors (v1a)

All pure functions in `backend/app/services/tactics/detectors.py`,
operating on the last N minutes of track points + cached context. Each
has a per-type cooldown (no repeat within X min) and a global race-level
cooldown (max ~1 call / 3 min) so the cockpit stays quiet.

Maneuver-class (predictive — fire on projected ETA):

1. **Planned-maneuver countdown** — the active isochrone route already
   encodes its heading changes; project the boat's progress along the
   plan and announce upcoming planned tacks/gybes when ETA enters the
   announce window ("plan calls for a tack in ~3 min"). Cheapest source
   of proactive calls — zero inference, pure plan-vs-position math.
2. **Layline approach** — from live position, next mark (existing
   `computeGuidance`), and current tack's polar VMG angles: project the
   layline crossing time from current VMG; fire when ETA enters the
   announce window, suppress if already inside `min_lead`.
3. **Forecast shift ahead** — look *forward* along the remaining route
   in the `WindForecast` grid: fire when the forecast shows a header/
   lift/pressure change reaching the boat within the next ~5–15 min
   ("breeze goes right ~15° in about 10 min — set up for the lift").
   Purely predictive; needs no observed trend at all.

Coaching-class (trim & helm — persistent-condition calls, 2026-06-11):

These are not retrospective diagnostics; they are corrective calls about
a condition that is true *right now* and stays actionable while it
persists. Their timeliness rule is a **persistence check** instead of an
ETA: the condition is re-verified against the latest batch at publish
(if the helm already corrected, the call is dropped), one call plus at
most one reminder per episode, then silence until the state changes.
Each detector emits a diagnosis AND a candidate-adjustment set from a
per-boat-class rule table; Claude phrases the correction, it does not
invent it.

4. **Pinching / sailing low** — inferred TWA (virtual wind inference per
   dev plan §2.1) vs the polar's target upwind/downwind angle, sustained
   > N° for > 45s. GPS-only — ships in v1a. Candidate adjustments:
   foot off / head up to target angle, with the delta in degrees.
5. **Over-heel** — sustained heel (rolling 30–60s median of OS-fused
   attitude, dock-zeroed) vs the boat's optimal-heel band for current
   TWS and point of sail (e.g. 18–22° upwind for the Beneteau 36.7,
   dev plan §2.1). Candidate adjustments per rule table: traveler down,
   ease main, flatten, reef by TWS band. Guarded by a **mount-quality
   gate**: implausible heel variance over the window (loose/pocketed
   phone) suppresses heel calls for the session; a baseline jump
   mid-race triggers a re-zero nudge. **v1c** — see phasing; the gate
   is heel-band data + one on-water sanity check, NOT custom sensor
   fusion (see below).
6. **Off-pace (catch-all)** — rolling 60s mean SOG vs polar target when
   neither 4 nor 5 explains it. Fires on sustained deficit > threshold
   (e.g. 12% for 90s). Suppressed during tacks/gybes (heading-change
   window).
7. **Observed shift / route divergence** — observed-implied wind departs
   the forecast by > N° sustained, or cross-track error vs the plan
   exceeds threshold. Primary action is triggering a route recompute
   (existing better-route machinery, not duplicated); the call itself
   says the *plan changed*, not what already happened.

v1b adds: **competitor delta** — nearby AIS vessels (existing
`ais.py` Redis cache) on the same leg with sustained SOG/VMG advantage,
grouped by side of course.

## Snapshot → Claude

A single JSON document (~1–2k tokens): race meta (boat class, polar id,
leg number, next mark bearing/distance), last 5 min of decimated track
(time, SOG, COG), current + forecast wind at position, polar targets for
the current TWA, active route summary, the detector(s) that fired with
their numbers, and the last 3 calls already made (so Claude doesn't
repeat itself).

System prompt contract (PROMPT_VERSION'd like the recap):
- Output ≤ 140 chars, one actionable sentence, no preamble — it renders
  as a notification.
- Maneuver calls MUST state time-to-event ("in about 3 minutes") taken
  from the snapshot's ETA — never a bare imperative with no horizon.
- Coaching calls MUST name a specific adjustment drawn from the
  snapshot's candidate-adjustment set ("traveler down, you're at 26°
  heel") — never just the symptom.
- May respond `SILENT` if the situation doesn't warrant a call; the
  pipeline drops it. This is the safety valve that makes detector
  thresholds forgiving.
- Never invent numbers not present in the snapshot.
- Tone: calm race-coach, no exclamation marks.

Note the staleness guard is enforced in code at publish time (ETA
re-checked after the Claude call returns), not delegated to the prompt.

Model: Haiku for v1 (recap already uses it; latency ~1s). Revisit Sonnet
only if on-water quality demands it.

## Delivery + mobile UI

- Extend the SSE payload with `type: "tactics"` alongside the existing
  better-route message; replay-latest-on-connect already works.
- `useRouteNotifications` gains a `tactics` stream; new
  `useTacticianCalls` hook keeps the last ~5 calls in memory.
- Recording screen: a dismissible TacticianCard (large type, glove-sized
  dismiss target) + a local notification when the app is backgrounded
  (reuse `raceCategories` pattern). No sound; haptic pulse only —
  "quiet cockpit" per dev plan §2.2.
- Per-race toggle next to the existing auto-route toggle (AsyncStorage
  + backend `app_settings` sync, matching the settings pattern).

## Gating + persistence

- Pro-only: evaluate `tier` at telemetry ingest (already on the request
  context); free users simply never trigger the pipeline.
- Persist calls to a `tactician_calls` JSONB column or table keyed by
  race session (decide at migration time) so the post-race Review screen
  can show "calls made vs what happened" — feeds the recap prompt too.

## Phasing

- **v1a (2–3 sessions):** detectors 1–4 + snapshot + Claude + SSE +
  TacticianCard + notification. **Plus IMU capture + upload** — the
  backend already accepts IMU batches (`imu_samples`, migration 0004)
  and the phone already reads DeviceMotion at 5 Hz for the heel gauge;
  the recorder just doesn't include the samples in the batch. Upload
  OS-fused attitude decimated to 2–5 Hz, alongside the calibration
  payload (schema already carries it) so the server can recompute
  boat-frame heel from raw orientation if calibration changes. Heel
  lands in the post-race Review immediately and v1c builds directly
  on this stream.
- **v1b (1–2 sessions):** AIS competitor detector + snapshot extension.
- **v1c (revised 2026-06-11 — ~1 session after v1a, not a distant
  phase):** heel-based advice calls. The dev plan §3.5 Kalman/
  complementary filter requirement is **superseded**: `useHeelGauge`
  already reads OS-fused attitude (CoreMotion / Android rotation
  vector — gyro+accel fusion done by the platform), boat-frame remap +
  dock-zero calibration shipped 2026-05-28, and trim calls need
  *sustained* heel (30–60s rolling median, which averages wave motion)
  rather than instantaneous heel. OS fusion + dock zero + windowed
  median is at least bubble-gauge accurate, which is the instrument
  this call class replaces. Remaining work: sustained-heel statistic +
  over-heel detector server-side, the mount-quality gate, the
  optimal-heel band table (the real blocker — data sourcing, see open
  question 6), and one on-water sanity race comparing logged sustained
  heel against crew observation (go/no-go).
- **WebSocket: deferred with rationale, not just sequencing.** The
  latency chain is batch (~10s) + detector evidence window (60–90s
  sustained) + Claude (~1–2s) + push; transport is the smallest term.
  A WS saves ~5–10s on calls that inherently take a minute to be
  trustworthy. It becomes worth building only for boat-handling-class
  calls ("ease now"), which also require continuous 5–10 Hz telemetry
  *upstream* — a different pipeline with real battery/data costs.
  Cheap latency lever available first: shrink the mobile flush
  interval (e.g. 10s → 5s) — see open question 1.

## Testing

- Detectors: pure-function unit tests with synthetic tracks (match
  `test_mark_rounding.py` style; replay the Dog Walk fixture).
- Pipeline: mocked-Anthropic orchestration tests (match
  `test_race_summary.py`); assert SILENT handling, cooldowns, Pro gating.
- Prompt: golden-snapshot tests pinning PROMPT_VERSION;
  optional real-API smoke behind `SAILLINE_AI_SMOKE=1`.
- On-water: log every detector fire + Claude response (including SILENT)
  to the session so threshold tuning uses real races, not guesses.

## Open questions (resolve before coding)

1. ~~Telemetry batch flush interval~~ ANSWERED (build, 2026-06-11):
   30 s (or immediately at 100 queued points) in JS mode; native mode
   is Transistorsoft's autoSync cadence. 30 s is the latency floor —
   shrinking it is the cheap lever if calls feel slow on the water.
2. ~~Inline vs offloaded~~ ANSWERED (build): in-process
   ``asyncio.create_task`` from the telemetry handler; the pipeline
   swallows all failures and the Claude call runs via
   ``asyncio.to_thread``.
3. Cooldown values (3 min global?) — pick starting numbers, tune on water.
4. Where calls persist (column on race_sessions vs new table).
5. Lead-time numbers: min_lead (~90s?) and announce window (2–4 min?)
   per call type — crew-size dependent; pick starting values, tune on
   water. Also whether min_lead should be a per-boat setting (a
   shorthanded crew needs more warning than a full crew).
6. Optimal-heel band + trim rule table per boat class: where does the
   data come from? (Class associations / polar providers publish some;
   18–22° Beneteau 36.7 is in the dev plan.) Likely a new CSV next to
   the polars keyed by TWS band — same sourcing problem as the polar
   gap, worth solving together.

## Tech-debt guardrails

- Detectors share the virtual-wind inference with nothing yet — build it
  as a standalone service module so the HUD (Phase 4) reuses it.
- Don't duplicate better-route logic in the divergence detector; it
  should only *reference* the active route.
