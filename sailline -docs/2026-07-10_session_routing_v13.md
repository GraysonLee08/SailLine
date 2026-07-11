# Session summary — 2026-07-10 (pre-Mackinac routing fix, engine v13)

## What we worked on

Investigated the rapid tack/gybe "staircase" visible in the computed
2026 Race to Mackinac route when zoomed in, confirmed it was an engine
artifact rather than a real recommendation, and shipped engine v13 to
fix it. Also traced and sanity-checked the 60h45m ETA.

## Root cause (three layers)

1. **No maneuver cost (v12 and earlier).** Two long boards and a
   staircase of one-step boards took identical modeled time, so the
   engine was indifferent. The Hagiwara bearing-bin culling then
   systematically preferred lineages that alternate sides every step
   (hugging the direct bearing to the finish).
2. **Time quantization swallows small penalties.** At dt=5min, a tack
   penalty (~46 m of progress) is far below the 5-minute arrival
   quantum — lineages with a dozen extra tacks still reach the finish
   on the same iteration, and the winner among same-iteration reachers
   was picked in arbitrary bin order.
3. **Bin geometry induces a tack cadence.** Closing speed on the finish
   *point* is maximized on the direct bearing, so even penalized
   searches weave with a period tied to bin width.

Bonus finding: `tack_count` only registers heading changes strictly
>60°; downwind gybes between hot angles (150°↔210°) are exactly 60°
apart and were never counted — which is how the staircase shipped
without tripping the metric. The card's TACKS number undercounts
downwind gybes (open item).

## Fix — engine v13-maneuver

`backend/app/services/routing/isochrone.py`:

- Tack penalty 15 s / gybe penalty 25 s (engine kwargs,
  `DEFAULT_TACK_PENALTY_S` / `DEFAULT_GYBE_PENALTY_S`), applied by
  shortening the maneuvering step's distance. Wind-side tracked per
  node (`_Node.side`); a maneuver is a side sign-flip vs. parent.
- Frontier keeps top-2 per bearing bin (`DEFAULT_FRONTIER_PER_BIN=2`,
  kwarg) because parent-dependent step cost breaks strict per-bin
  dominance.
- Culling score charges each lineage `BIN_TIEBREAK_M` (25 m) per
  *cumulative* maneuver.
- Finish selection among same-iteration reachers: fewest cumulative
  maneuvers, then closest.
- `_consolidate_boards` post-pass: within 24-step (2 h) windows,
  stably reorders step displacement vectors to cluster same-tack
  boards. Total time and endpoints exactly unchanged; every reordered
  segment re-checked against navigability and the rounding filter;
  failing windows keep original order.
- Setting penalties to 0 and `frontier_per_bin=1` reproduces v12
  exactly (consolidation still applies).

Results (uniform-wind synthetic, 30 nm legs): upwind beat 15→4 side
flips, downwind run →3, multileg →6, identical total minutes.

## Files changed

- `backend/app/services/routing/isochrone.py` — engine v13 (all of the above)
- `backend/app/services/routing/pipeline.py` — `ENGINE_VERSION = "v13-maneuver"` + changelog docstring
- `backend/tests/test_engine_version.py` — pinned version updated
- `backend/tests/test_isochrone_maneuver.py` — new; counts wind-side flips (not `tack_count`) across down/up/multileg scenarios, penalty-monotonicity, legacy-knob compat
- `backend/tests/test_routing_router_currents.py` — duplicate version pin updated to v13 (fixed by Grayson; noted as stale duplicate of the canonical fingerprint test)

## Verification

Full backend suite on Windows: **931 passed** (after the duplicate-pin
fix), 6 deselected. Sandbox-side: standalone engine repro mirrored all
13 relevant test scenarios green. Sandbox pytest against the E:\ mount
remains unusable (stale/truncated reads — see memory notes).

## ETA sanity check (60h45m)

ETA = 5 min × engine steps along the winning path; all physics is in
step distances (polar(TWA, GFS TWS) × 0.514 × 300 s × 0.97 margin +
OFS current drift). 289 nm at 60.75 h = 4.8 kt avg SOG — consistent
with the 8 kt start wind and a light GFS window sailed at VMG angles,
not with 15–20 kt (which would put the race near 40–45 h). If stronger
wind verifies in later GFS cycles, the ETA will drop on recompute and
the recompute worker will push better-route alerts. v13 prices
maneuvers into the ETA (~+20 s each) — honest direction, negligible
magnitude.

## Decisions made

- Penalties are physical constants (15/25 s), not tuned to make tests
  pass; the consolidation pass is the mechanism that fixes the
  sub-quantum weave. Thresholds in the new tests (≤6/≤8 flips) pass
  with margin (3–6 actual).
- Top-2 per bin (not 3): penalty + consolidation do the real work;
  K is a kwarg if we want to experiment.
- ENGINE_VERSION bump invalidates all cached routes on deploy —
  intentional pre-race.

## Open items / next steps

- **Deploy tonight** (race starts Sat 11 Jul 11:20): push to main,
  Cloud Build gates on tests, then Recompute the Mac route and
  visually verify consolidated boards + compare new ETA.
- Check route meta on the fresh compute: `currents_quality` (LMHOFS
  may silently be off), `forecast_quality`, `horizon_exceeded`.
- `tack_count` undercounts downwind gybes (>60° strict). Consider a
  wind-side-flip count in RouteResult for the TACKS card.
- Consolidation displaces board timing by up to 2 h within a window —
  fine under slowly-evolving GFS, worth revisiting if we ever shrink
  dt or route in frontal conditions.
- Development plan `.docx` NOT updated this session — the sandbox
  mount of E:\ is unreliable for binary reads and editing the docx
  through it risks corruption. Update it from Windows next session.
- Duplicate ENGINE_VERSION pin existed in
  `test_routing_router_currents.py`; consider deleting the duplicate
  in favor of the canonical `test_engine_version.py`.

## Technical debt flagged

- None added. The v13 knobs are engine kwargs with defaults; pipeline
  passes none explicitly. The consolidation window constant
  (`CONSOLIDATION_WINDOW_STEPS=24`) is module-level, not exposed as a
  kwarg — deliberate, revisit only if a real need appears.
