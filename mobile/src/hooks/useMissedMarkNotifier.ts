// useMissedMarkNotifier.ts — watch boat motion vs. next-expected mark
// and fire the "missed mark?" notification if the v3 detector likely
// missed.
//
// Heuristic (chosen for tomorrow's Waukegan-to-Burnham delivery test):
//   * Sailor is recording.
//   * Next-expected mark exists (not done).
//   * Boat has come within APPROACH_M of the next mark at some point in
//     the recent past (kept as a running minimum).
//   * Boat is now MORE than RECEDE_M away AND moving away (current
//     distance > running minimum + RECEDE_BUFFER_M).
//   * No pass has been emitted for this mark.
//   * No notification has been fired in the last COOLDOWN_MS.
//
// Why these constants: the v3 detector fires within ~15 s of CPA when
// the boat is < CPA threshold (250 m distance / 100 m inshore). If it
// HASN'T fired and the boat is 500+ m past the running minimum, the
// most likely explanation is the running minimum was outside threshold
// (a wide-but-not-wide-enough pass). The notifier should ask.
//
// Reset state when:
//   * Recording stops.
//   * Next-expected mark advances (a pass was recorded, auto or manual).
//   * Race id changes.
//
// Watch reach: the notification uses CATEGORY_MISSED_MARK so action
// buttons appear on supported wearables. The response listener in
// App.tsx routes button taps back to the manual-pass API.

import { useEffect, useMemo, useRef } from "react";

import type { MarkPass } from "../api/races";
import {
  dismissMissedMarkNotification,
  postMissedMarkNotification,
} from "../notifications/missedMark";
import type { RaceMark } from "../types";

// Tunables — see module docstring for the rationale.
const APPROACH_M = 500; // running-min must drop below this to arm
const RECEDE_M = 750; // current distance must exceed this to fire
const RECEDE_BUFFER_M = 400; // current must also be running_min + this
const COOLDOWN_MS = 3 * 60 * 1000; // don't re-fire within 3 min for same mark

type Options = {
  raceId: string | null;
  recording: boolean;
  marks: RaceMark[];
  passes: MarkPass[];
  /** Recorder's most recent fix. */
  lastPoint: { lat: number; lon: number } | null;
  /**
   * When false the hook still mounts (rules of hooks) but never arms
   * its running-min logic and never fires a notification. Drives the
   * global "auto-pass" toggle in Settings. Default true preserves the
   * existing behaviour for any caller that hasn't been migrated.
   * 2026-06-03 B2.
   */
  enabled?: boolean;
};

// Haversine duplicated here to keep this module free of the @sailline/shared
// import chain (the v3 detector lives in shared but pulls in distance math
// + types that don't matter for this hook).
function haversineM(
  lat1: number,
  lon1: number,
  lat2: number,
  lon2: number,
): number {
  const R = 6_371_000;
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dp = ((lat2 - lat1) * Math.PI) / 180;
  const dl = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dp / 2) ** 2 +
    Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

export function useMissedMarkNotifier({
  raceId,
  recording,
  marks,
  passes,
  lastPoint,
  enabled = true,
}: Options): void {
  // Next-expected mark index. Same logic as MarkPassControls.
  const nextIdx = useMemo(() => {
    const seen = new Set<number>(passes.map((p) => p.mark_index));
    for (let i = 0; i < marks.length; i += 1) {
      if (!seen.has(i)) return i;
    }
    return marks.length;
  }, [marks.length, passes]);

  // Tracked across renders without re-firing the effect.
  const runningMinRef = useRef<number | null>(null);
  const lastFireRef = useRef<{ markIndex: number; firedAt: number } | null>(null);
  const armedMarkRef = useRef<number | null>(null);

  // Reset whenever the context fundamentally changes. Toggling the
  // global enabled flag also resets so re-enabling mid-race doesn't
  // fire on a stale running-min from before the user turned it off.
  useEffect(() => {
    runningMinRef.current = null;
    armedMarkRef.current = null;
    lastFireRef.current = null;
    if (raceId) void dismissMissedMarkNotification(raceId);
  }, [raceId, nextIdx, enabled]);

  useEffect(() => {
    if (!enabled) return; // B2 — global "auto-pass" toggle is OFF.
    if (!recording) return;
    if (!raceId) return;
    if (!lastPoint) return;
    if (nextIdx >= marks.length) return;

    const target = marks[nextIdx];
    if (!target || !Number.isFinite(target.lat) || !Number.isFinite(target.lon)) {
      return;
    }

    const d = haversineM(lastPoint.lat, lastPoint.lon, target.lat, target.lon);

    // Update running minimum for this mark.
    if (
      runningMinRef.current === null ||
      d < runningMinRef.current
    ) {
      runningMinRef.current = d;
    }

    // Arm once the boat gets close enough that a "wide pass" is even
    // plausible. Prevents a fresh course-start from firing the notifier
    // before the boat has approached any mark.
    if (
      armedMarkRef.current !== nextIdx &&
      runningMinRef.current !== null &&
      runningMinRef.current <= APPROACH_M
    ) {
      armedMarkRef.current = nextIdx;
    }

    if (armedMarkRef.current !== nextIdx) return;

    const min = runningMinRef.current ?? Number.POSITIVE_INFINITY;
    const recedingClear = d > RECEDE_M && d > min + RECEDE_BUFFER_M;

    if (!recedingClear) return;

    // Cooldown — never re-fire for the same mark within COOLDOWN_MS.
    const now = Date.now();
    const last = lastFireRef.current;
    if (last && last.markIndex === nextIdx && now - last.firedAt < COOLDOWN_MS) {
      return;
    }

    lastFireRef.current = { markIndex: nextIdx, firedAt: now };
    void postMissedMarkNotification({
      raceId,
      markIndex: nextIdx,
      markName: target.name,
    });
  }, [
    enabled,
    raceId,
    recording,
    marks,
    nextIdx,
    lastPoint?.lat,
    lastPoint?.lon,
  ]);
}
