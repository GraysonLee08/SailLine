// hooks/useNextMarkGuidance.ts — live "next mark" computation.
//
// Wraps the pure `computeGuidance` from @sailline/shared with the
// recorder's in-memory point buffer. Updates when the buffer grows —
// Transistorsoft pushes ~1 point/sec, so the GuidanceCard refreshes
// ~1Hz during racing.
//
// Throttle (2026-06-30): the original implementation fed ALL points
// through the detector on every new fix — O(N) per fix, O(N²) over a
// race. With the breadcrumb cap at 5000, that's 5000 points processed
// 5000 times. Now we only feed the last 120 points (~2 minutes of
// track at 1 Hz) since mark detection only cares about recent
// proximity. The detector determines "next expected mark" from the
// mark_passes sequence, not from replaying the full track. We also
// gate on `points.length` instead of the array reference so identical
// lengths don't re-run (the cap means the array stops growing at 5000
// but the content still changes — so after 5000 we also gate on
// `lastPoint` to catch the latest fix).

import { useMemo } from "react";

import { computeGuidance, radiiForCourse } from "@sailline/shared";

import type { LocalPoint } from "../recorder/backgroundGeolocation";
import type { Race } from "../types";

// How many recent points to feed the detector. 120 = ~2 min at 1 Hz.
// Mark detection is proximity-based — only recent track matters.
const RECENT_POINTS_WINDOW = 120;

type Args = {
  race: Race | null;
  points: ReadonlyArray<LocalPoint>;
  lastPoint: LocalPoint | null;
};

export function useNextMarkGuidance({ race, points, lastPoint }: Args) {
  const pointsLen = points.length;
  return useMemo(() => {
    if (!race || !lastPoint || race.marks.length === 0) return null;
    // Feed only the last N points to the detector. Mark detection
    // checks proximity to the next expected mark — it doesn't need
    // the full race history. This caps the per-fix cost at O(120)
    // instead of O(5000).
    const start = Math.max(0, pointsLen - RECENT_POINTS_WINDOW);
    const recent = points.slice(start);
    const detectorPoints = recent.map((p) => ({
      lat: p.lat,
      lon: p.lon,
      ts: p.recorded_at,
    }));
    return computeGuidance({
      marks: race.marks,
      points: detectorPoints,
      current: { lat: lastPoint.lat, lon: lastPoint.lon },
      radiusM: radiiForCourse(race.marks.length),
    });
    // Gate on pointsLen (not the array reference) + lastPoint (catches
    // new fixes after the array hits the 5000 cap and stops growing).
  }, [race, pointsLen, lastPoint, points]);
}
