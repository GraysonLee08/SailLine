# Session summary — 2026-07-09 (evening): Route truncation fix + recompute overhaul (v12-fullrace)

## What we worked on

Investigated why the 2026 Race to Mackinac route stopped mid-lake at
20h55m instead of reaching the finish, then shipped the fix plus four
related items (all five approved as one batch).

### Root cause of the truncation

**Not** the weather-model handoff — HRRR→GFS blending and course-sized
forecast windows already worked. The isochrone engine has
`max_iterations=240` at `dt_minutes=5.0` = a hard **20h simulated-sailing
cap per leg**, and `pipeline.py` never overrode the default. The Mac
route = 55min leg 1 + exactly 20h00m of leg 2 before the cap → partial
route, `reached=False`, closest-approach fallback. Leftover from the era
of the fixed 6h forecast window.

Secondary finds: the Δ53° "wind shifted" banner fired 40h before the gun
(the freshness check is only valid in the T-5 window it was designed
for, but `showFreshness` had no time gate); `reached=False` routes were
cached for 1h; the recompute worker skipped races starting >24h out and
in-progress races >2h after the gun, only published on ≥5% improvement,
and always routed from the start line.

## Changes shipped (ENGINE_VERSION → `v12-fullrace`)

1. **Course-sized engine budget** — `max_iterations` is now a TOTAL
   simulated-time budget across legs (multileg threads the remainder to
   each leg). Pipeline sizes it: `min(2 × max(course_est_h, window_h),
   240h)` at 5-min steps via `simulated_time_budget_iterations()`.
2. **Persist-last-frame wind** — `WindForecast(persist_beyond_horizon=True)`
   returns the last snapshot's wind past `t_max` instead of `None`.
   Loader coverage rule relaxed in persist mode: GFS only needs to reach
   race START. Meta gains `forecast_t_max` + `horizon_exceeded`.
3. **Recompute worker keyed to route time** — pre-start: any unfinished
   race whose owner computed at least once (`route:last_request` exists),
   no hour cutoff (SQL bound: start within +120h / started within 7d);
   in-progress: eligible until `start_at + baseline_minutes × 1.5`.
   **Mid-race routes from the latest `track_points` fix to the remaining
   marks** (`marks[len(mark_passes):]`), `race_start=now`, no cache.
   Publishes on EVERY successful pass with `kind: "update"|"better"`;
   baselines are phase-scoped JSON (`{total_minutes, phase}`, legacy
   bare-float parsed as phase "pre").
4. **Frontend** — `showFreshness` gated on `inPreStartWindow` (T-5);
   `useRouteNotifications` splits `update` (auto-applied to the map via
   `routing.applyAlternative`) from `alternative` (banner, unchanged);
   payloads without `kind` treated as "better" for back-compat.
5. **Cache policy** — `reached=False` results are never written to the
   route cache (logged instead).

## Files changed

Backend:
- `backend/app/services/routing/isochrone.py` — multileg total-budget semantics
- `backend/app/services/routing/wind_forecast.py` — `persist_beyond_horizon`
- `backend/app/services/weather/forecast_loader.py` — persist-mode coverage rule + flag passthrough
- `backend/app/services/routing/pipeline.py` — v12 bump, `estimate_course_hours`, `simulated_time_budget_iterations`, budget + dt passed to engine, `persist_beyond_horizon=True`, horizon meta, unreached-not-cached
- `backend/app/services/routing/__init__.py` — export `estimate_course_hours`
- `backend/workers/route_recompute.py` — rewritten (selection, live origin, kind publish)

Frontend:
- `frontend/src/hooks/useRouteNotifications.js` — `update` vs `alternative` split
- `frontend/src/components/MapView.jsx` — auto-apply effect + freshness gate

Tests:
- `backend/tests/test_engine_version.py` — pinned `v12-fullrace`
- `backend/tests/test_wind_forecast.py` — persistence (4 new)
- `backend/tests/test_forecast_loader.py` — persist coverage rule (3 new)
- `backend/tests/test_routing_pipeline.py` — budget sizing, persist flag, horizon meta, unreached cache skip (6 new)
- `backend/tests/test_isochrone_multileg.py` — total-budget semantics (2 new)
- `backend/tests/test_route_recompute.py` — rewritten for v12 semantics

## Decisions made

- **Mid-race reroute origin = live GPS fix** (Grayson's call), remaining
  marks derived from `mark_passes` length — no new state needed; falls
  back to full course when no telemetry exists.
- **Pre-start eligibility = "has a computed route, not finished"** — the
  `route:last_request` blob (7d TTL) is the signal; avoids recomputing
  races nobody ever routed.
- Budget factor 2×, ceiling 240h simulated; persistence is forward-only
  (pre-`t_min` still None).
- SSE event name stays `alternative` for both kinds (the router's
  `_event_name_for` only branches on `type`, which tactics uses);
  frontend branches on `kind` inside the payload.
- No migration needed — all required state already existed.

## Open items / next steps

- **Windows verification (required, sandbox can't run these):**
  - `cd E:\Personal\Coding\SailLine\backend; pytest -m "not slow"`
  - `cd E:\Personal\Coding\SailLine\frontend; npm test`
  - After deploy: Recompute the Mac race, confirm the magenta line
    reaches the island and `horizon_exceeded` behaves.
- Mobile app consumes the same SSE stream? Verify the RN client handles
  `kind: "update"` (this session only touched the web frontend).
- Consider surfacing `horizon_exceeded` in the UI (e.g. dashed route tail
  past `forecast_t_max`).
- Recompute worker trigger cadence unchanged (post-ingest); the wider
  selection means more computes per pass — watch job duration.
- Prod ingest freshness worth a look: the truncated route was served
  "cached", which requires the newest GFS cycle to be the same one — fine
  if ingest is healthy, but see 2026-07-09 walk-test note re: prod drift.

## Technical debt flagged

- `route:alternative` now stores whichever kind was last published; a
  reconnecting client may auto-apply an "update" that predates a
  dismissed "better" (minor, newest-wins is defensible).
- Live-phase recompute uses `now` as `race_start`, so the published
  `total_minutes` is remaining-course time — any consumer comparing it
  to full-course numbers must check `phase`.
- `estimate_course_hours` double-duties as ETA guard for worker
  eligibility; a proper ETA (from the last computed route's remaining
  time) would be tighter.
