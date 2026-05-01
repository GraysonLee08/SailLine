# Session — Adaptive wind barb density

**Date:** May 1, 2026.
**Status:** Shipped.

## Problem

Near-shore race courses (4–5 nm wide) showed zero wind barbs at typical
zoom (~13). Diagnosis: not the `SUBSAMPLE` constant (already at 1) — the
upstream resolution. HRRR's native ~3km grid is regridded by the worker
to 0.1° (~11km), so a typical race-area-sized viewport contains 0–1
native points. Even rendering every native point isn't enough.

## What we built

Adaptive density rendering with bilinear interpolation underneath
(Option 3 over Option 2 from the original triage). Aims for ~constant
on-screen barb spacing (~70px) regardless of zoom.

### Files changed

| File | Change |
|---|---|
| `frontend/src/lib/windBarb.js` | Added `bilerpUV(weather, lat, lon)` for sampling u/v at arbitrary points; `findBracketIndex` binary-search helper that handles both ascending and descending coord arrays (HRRR lats can come either direction). |
| `frontend/src/components/MapView.jsx` | Replaced the fixed `SUBSAMPLE` double-loop with `computeFeatures(map, weather)`. Recomputes on `moveend` (covers both pan and zoom, fires once after the gesture settles — no manual debouncing needed). |

### Algorithm

1. Compute `pxPerDeg` from current zoom + `cos(centerLat)` (Web Mercator).
2. `targetDeg = 70 / pxPerDeg`.
3. **If `targetDeg ≥ nativeStep`** (zoomed out): stride the native grid
   by `round(targetDeg / nativeStep)`, clip to viewport bounds.
4. **If `targetDeg < nativeStep`** (zoomed in): walk a synthetic grid
   at `targetDeg` spacing across the viewport, sample via `bilerpUV`.
5. Snap the synthetic grid origin to a multiple of `targetDeg` so barbs
   stay put while panning instead of crawling (anti-shimmer).

### Verified by inspection

| Zoom | `targetDeg` (at 42°N) | Mode | Behavior |
|---|---|---|---|
| 9 | ~0.26° | Stride native, stride=3 | ~native field, decimated |
| 11 | ~0.065° | Interpolated | Denser than native |
| 13 | ~0.016° | Interpolated | 4nm course shows ~5 barbs across |

## Decisions worth remembering

- **Interpolated values are honest about being a presentation layer.**
  Comment in `windBarb.js` calls out that bilerp adds smoothness, not
  information beyond ~11km. If anyone ships a "ground truth wind"
  feature later, this needs to come back through and either flag
  interpolated cells visually or fall back to the native grid.
- **`moveend` is the right event.** `move` fires per-frame during pans
  and would burn CPU; `zoomend` misses pans; `moveend` covers both with
  natural coalescing. No throttle needed.
- **No design-skill consult.** Barb visuals didn't change, only
  positions and density. Pure data-flow change.
- **Removed the `SUBSAMPLE` constant entirely.** Its job is now done by
  `targetDeg / nativeStep` ratio, which is zoom-aware.

## Open items

- **Worker-side resolution bump (Option 1) still on the table.** If
  interpolation artifacts become noticeable in the field at race
  distances, drop the worker regrid step from 0.1° to 0.03° (~3km,
  matches HRRR native). One-line worker change. Payload goes from
  ~150KB gzipped to ~1.5MB. Defer until a real race gives us evidence
  the smoothing is misleading.
- **Race-area-aware density override.** When a race is loaded, could
  force interpolated mode within the course bbox regardless of zoom so
  the editor view is always informative. Not needed yet — adaptive
  density already covers this once the user zooms in.
- **Edge of grid.** `bilerpUV` returns null outside the source bbox, so
  panning to the edge of the great_lakes region just shows fewer barbs.
  Acceptable; document if anyone asks.

## Carried forward (still outstanding from prior sessions)

Same backlog as before — none of these were touched:

- Region/source pickers in the UI (still hardcoded great_lakes / HRRR).
- Legend showing barb conventions (full flag = 10kt, etc).
- Smooth crossfade between forecast frames when time-scrubbing lands.
- After saving a race, load directly into the map view.
- Race date / class start time fields with countdown in the editor.
