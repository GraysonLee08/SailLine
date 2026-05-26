// hooks/useNextMarkGuidance.ts — live "next mark" computation.
//
// Wraps the pure `computeGuidance` from @sailline/shared with the
// recorder's in-memory point buffer. Updates every render the buffer
// grows — Transistorsoft pushes ~1 point/sec, so the GuidanceCard
// effectively refreshes 1Hz during racing.
//
// Why no memoization on `points`: the buffer is replaced (not mutated)
// inside the recorder, so React's reference-equality re-render check
// already gates on actual change. Recomputing the detector replay on
// every push is cheap relative to the rest of the recording loop.

import { useMemo } from "react";

import { computeGuidance, radiiForCourse } from "@sailline/shared";

import type { LocalPoint } from "../recorder/backgroundGeolocation";
import type { Race } from "../types";

type Args = {
  race: Race | null;
  points: ReadonlyArray<LocalPoint>;
  lastPoint: LocalPoint | null;
};

export function useNextMarkGuidance({ race, points, lastPoint }: Args) {
  return useMemo(() => {
    if (!race || !lastPoint || race.marks.length === 0) return null;
    // Adapt LocalPoint (recorded_at) -> detector input shape (ts).
    // Allocation is fine: this hook only re-runs when `points` changes,
    // and the detector iterates the array once.
    const detectorPoints = points.map((p) => ({
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
  }, [race, points, lastPoint]);
}
