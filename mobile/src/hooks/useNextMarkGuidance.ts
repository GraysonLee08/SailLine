// hooks/useNextMarkGuidance.ts — live "next mark" computation.
//
// Wraps the pure `computeGuidance` from @sailline/shared with the
// recorder's in-memory point buffer. Updates when the buffer grows —
// Transistorsoft pushes ~1 point/sec, so the GuidanceCard refreshes
// ~1Hz during racing.
//
// SERVER-AUTHORITATIVE NEXT MARK (2026-07-10): `serverNextMarkIndex`
// is the persisted mark_passes count from `useMarkPasses` (15 s poll
// while recording). When it's available, computeGuidance skips the
// local detector replay entirely — the server's v4 gate detector is
// the single source of truth for "which mark is next", exactly like
// auto-stop already trusts server `ended_at`.
//
// Why the local replay had to go: the 2026-06-30 perf throttle fed
// only the last 120 points (~2 min) into a FRESH detector on every
// fix. Once a rounding scrolled out of that window the replay found
// no passes and the card regressed to "next mark: Start" — the
// 2026-07-10 Last Test bug. The replay (over the same 120-point
// window) is kept ONLY as a fallback for the moments before the first
// useMarkPasses fetch resolves (or when polling is unavailable);
// wrong-but-monotonic beats blank at mount, and the server value
// takes over within one poll cycle.
//
// Nav math (bearing / distance / cross-track) still runs locally at
// 1 Hz off the latest fix — only the index comes from the server.

import { useMemo } from "react";

import { computeGuidance, radiiForCourse } from "@sailline/shared";

import type { LocalPoint } from "../recorder/backgroundGeolocation";
import type { Race } from "../types";

// How many recent points to feed the FALLBACK detector. 120 = ~2 min
// at 1 Hz. Only used until the first server pass-list fetch lands.
const RECENT_POINTS_WINDOW = 120;

type Args = {
  race: Race | null;
  points: ReadonlyArray<LocalPoint>;
  lastPoint: LocalPoint | null;
  /** Server-authoritative next-mark index — pass
   *  `markPasses.loading ? null : markPasses.passes.length` from
   *  `useMarkPasses`. `null`/`undefined` falls back to the local
   *  replay (pre-first-fetch only). */
  serverNextMarkIndex?: number | null;
};

export function useNextMarkGuidance({
  race,
  points,
  lastPoint,
  serverNextMarkIndex = null,
}: Args) {
  const pointsLen = points.length;
  return useMemo(() => {
    if (!race || !lastPoint || race.marks.length === 0) return null;

    if (
      typeof serverNextMarkIndex === "number" &&
      Number.isInteger(serverNextMarkIndex) &&
      serverNextMarkIndex >= 0
    ) {
      // Server mode — no replay, no points needed for the index.
      return computeGuidance({
        marks: race.marks,
        points: [],
        current: { lat: lastPoint.lat, lon: lastPoint.lon },
        nextMarkIndex: serverNextMarkIndex,
      });
    }

    // Fallback (first mount, before the pass poll resolves): replay
    // the last N points. Known-imperfect — see module docstring.
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
  }, [race, pointsLen, lastPoint, points, serverNextMarkIndex]);
}
