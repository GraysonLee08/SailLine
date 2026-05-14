// frontend/src/hooks/useAutoStopRecorder.js
//
// Auto-stop the recorder once the boat has finished the course.
//
// Trigger conditions (ALL must hold):
//   1. The recorder is currently `recording`.
//   2. Course has >= 2 marks (single-mark "race to" courses don't auto-
//      stop — there's no second-to-last to gate against).
//   3. Both the second-to-last AND the last mark in the course have
//      been rounded. The two-mark gate prevents premature auto-stop on
//      beer-can layouts where start == finish: crossing the start line
//      on lap 1 doesn't qualify because the last mark hasn't been hit
//      yet, and on the FINAL pass through the start it does qualify
//      because both mark[n-2] and mark[n-1] are rounded.
//   4. AUTO_STOP_DELAY_MS has elapsed since the last rounding (gives
//      the user a buffer to actually cross the line, drift past, and
//      coast across the finish before recording cuts off).
//
// Idempotency: once `stop()` has been called for a given (raceId, last-
// pass-key), the hook will not call stop again — the user can manually
// re-start recording afterwards without the hook re-firing.
//
// Mirrors the server's `mark_passes` shape via `lib/markRounding.js`.
// We intentionally recompute from `points` rather than reading the POST
// response; that keeps the recorder's hook independent of network state
// and means a flush failure doesn't delay auto-stop.

import { useEffect, useMemo, useRef, useState } from "react";

import { computePasses } from "../lib/markRounding";

const AUTO_STOP_DELAY_MS = 5 * 60 * 1000;

export function useAutoStopRecorder({
  raceId,
  marks,
  points,
  recording,
  stop,
  enabled = true,
  onFired = null,
}) {
  // Recompute mark passes whenever points change. computePasses is
  // pure and cheap: O(points * marks). At 1 Hz over a 4-hour race
  // that's ~50k mark distance evals — sub-millisecond.
  const passes = useMemo(() => {
    if (!enabled || !recording) return [];
    if (!Array.isArray(marks) || marks.length < 2) return [];
    if (!Array.isArray(points) || points.length === 0) return [];
    const cleanMarks = marks
      .filter(
        (m) => Number.isFinite(m?.lat) && Number.isFinite(m?.lon),
      )
      .map((m) => ({ lat: m.lat, lon: m.lon }));
    if (cleanMarks.length < 2) return [];
    return computePasses(cleanMarks, points);
  }, [marks, points, recording, enabled]);

  // Gate condition: both the last and second-to-last marks rounded.
  const courseLen = Array.isArray(marks) ? marks.length : 0;
  const lastIdx = courseLen - 1;
  const secondLastIdx = courseLen - 2;
  const hasLast = passes.some((p) => p.markIndex === lastIdx);
  const hasSecondLast = passes.some((p) => p.markIndex === secondLastIdx);
  const gateOpen = courseLen >= 2 && hasLast && hasSecondLast;

  // The latest pass timestamp anchors the 5-minute delay. We use the
  // MAX timestamp across all passes (not just the last in the array)
  // so out-of-order ingest doesn't shorten the delay window. In
  // practice they're the same — passes are emitted in chronological
  // order — but defensive.
  const lastPassTs = useMemo(() => {
    if (!gateOpen) return null;
    let max = 0;
    for (const p of passes) {
      const t = new Date(p.ts).getTime();
      if (Number.isFinite(t) && t > max) max = t;
    }
    return max || null;
  }, [passes, gateOpen]);

  // Idempotency key. Stays the same if the latest pass instant doesn't
  // change — protects against accidental re-firing on point appends
  // that don't add new passes.
  const firedKeyRef = useRef(null);
  const stopRef = useRef(stop);
  stopRef.current = stop;
  // Same ref pattern so callers don't have to memoise their onFired
  // handler — we always call the latest one.
  const onFiredRef = useRef(onFired);
  onFiredRef.current = onFired;

  const [armed, setArmed] = useState(false);
  const [msUntilStop, setMsUntilStop] = useState(null);

  useEffect(() => {
    setArmed(false);
    setMsUntilStop(null);

    if (!enabled || !recording) return;
    if (!gateOpen || !lastPassTs) return;

    const key = `${raceId}|${lastPassTs}`;
    if (firedKeyRef.current === key) return;

    const fireAt = lastPassTs + AUTO_STOP_DELAY_MS;
    const delay = fireAt - Date.now();

    const fire = () => {
      firedKeyRef.current = key;
      setArmed(false);
      setMsUntilStop(null);
      try {
        stopRef.current?.();
      } catch {
        /* recorder may have been torn down — ignore */
      }
      // Optional follow-up — used by AppView to navigate to
      // RaceStatsView once recording has cut off. Failure here is
      // never fatal; the stop above is the actual side effect.
      try {
        onFiredRef.current?.(raceId);
      } catch {
        /* navigation handler failed — log nothing, race is still stopped */
      }
    };

    if (delay <= 0) {
      // Already past the delay (e.g. user reopened the app long after
      // the race ended). Fire on the next tick so the order of state
      // transitions is observable in tests.
      const t = setTimeout(fire, 0);
      setArmed(true);
      setMsUntilStop(0);
      return () => clearTimeout(t);
    }

    setArmed(true);
    setMsUntilStop(delay);
    const t = setTimeout(fire, delay);
    return () => clearTimeout(t);
  }, [raceId, recording, enabled, gateOpen, lastPassTs]);

  return {
    /** Mark roundings detected from the in-memory points buffer. */
    passes,
    /** True while a stop timer is scheduled. */
    armed,
    /** Milliseconds until the scheduled stop fires; null when not armed. */
    msUntilStop,
    /** Whether the gate (last + second-to-last rounded) is open. */
    gateOpen,
  };
}
