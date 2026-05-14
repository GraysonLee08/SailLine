// frontend/src/hooks/useAutoStartRecorder.js
//
// Auto-fire `recorder.start()` 5 minutes before `race.start_at`.
//
// Lifecycle:
//   - Computes ms-until-arming from now. If positive, schedules a single
//     setTimeout to fire at exactly that instant. If already past the
//     arming instant when the hook mounts (or when start_at changes to
//     a value in the past <5min), fires immediately. If start_at is
//     more than ARM_WINDOW_MS in the past, doesn't fire — the race has
//     already started and we don't want to retroactively kick off a
//     stale recording session.
//   - Re-arms whenever start_at, enabled, or race id changes. Race start
//     time often slips by 5–15 minutes for inshore racing; if the user
//     pushes it back, the timer reschedules instead of firing at the
//     original instant.
//   - Idempotent against the recorder: if recording is already true at
//     fire time (user hit Record manually), do nothing.
//   - Only fires once per (raceId + start_at) combination. State key is
//     `${raceId}|${startAtIso}` so two races with the same start_at
//     don't collide, and editing start_at mid-day re-arms cleanly.
//
// Returned state:
//   - armed: true while a timer is scheduled (= we will fire later)
//   - fired: true after we've called recorder.start() for the current
//            (raceId, start_at). Stays true until the key changes.
//   - msUntilFire: ms until the scheduled fire; null when not armed.
//
// What the hook does NOT do:
//   - Doesn't watch for tab close / app background. setTimeout pauses
//     under hidden-tab throttling, so this only works if the user has
//     the tab/app open near gun time. Capacitor's background-execution
//     hooks (Session A) are what would let this fire while the app is
//     suspended — out of scope here.

import { useEffect, useRef, useState } from "react";

const ARM_OFFSET_MS = 5 * 60 * 1000;            // 5 minutes
const ARM_WINDOW_MS = 5 * 60 * 1000;            // don't retro-fire if >5min past start

export function useAutoStartRecorder({
  raceId,
  startAtIso,
  enabled,
  recording,
  start,
}) {
  const [armed, setArmed] = useState(false);
  const [fired, setFired] = useState(false);
  const [msUntilFire, setMsUntilFire] = useState(null);

  // Refs let the timer callback see fresh values without restarting.
  const recordingRef = useRef(recording);
  recordingRef.current = recording;
  const startRef = useRef(start);
  startRef.current = start;

  // Reset `fired` whenever the identifying key changes (new race,
  // re-scheduled start). Without this, editing start_at to a later
  // time after we already fired would prevent the re-arm from acting.
  const keyRef = useRef(null);

  useEffect(() => {
    const key = enabled && raceId && startAtIso ? `${raceId}|${startAtIso}` : null;
    if (key !== keyRef.current) {
      keyRef.current = key;
      setFired(false);
    }

    setArmed(false);
    setMsUntilFire(null);

    if (!key) return;

    const startAt = new Date(startAtIso).getTime();
    if (Number.isNaN(startAt)) return;

    const armAt = startAt - ARM_OFFSET_MS;
    const now = Date.now();
    const delay = armAt - now;

    // Fire immediately if we're already inside the 5-min window but the
    // race hasn't started yet, OR within ARM_WINDOW_MS of having passed
    // it (covers the "reload the tab 2 min before gun" case).
    if (delay <= 0) {
      const sincePast = -delay;
      if (sincePast > ARM_OFFSET_MS + ARM_WINDOW_MS) {
        // Race started long ago — don't auto-start, the user will
        // either start manually or has already finished.
        return;
      }
      // Fire on the next tick so consumers can observe armed→fired
      // transitions in a predictable order.
      const t = setTimeout(() => {
        if (!recordingRef.current) {
          try {
            startRef.current?.();
          } catch {
            /* recorder may throw if no raceId — silently swallow */
          }
        }
        setFired(true);
        setArmed(false);
        setMsUntilFire(null);
      }, 0);
      setArmed(true);
      setMsUntilFire(0);
      return () => clearTimeout(t);
    }

    setArmed(true);
    setMsUntilFire(delay);
    const t = setTimeout(() => {
      if (!recordingRef.current) {
        try {
          startRef.current?.();
        } catch {
          /* swallow */
        }
      }
      setFired(true);
      setArmed(false);
      setMsUntilFire(null);
    }, delay);

    return () => clearTimeout(t);
  }, [raceId, startAtIso, enabled]);

  return { armed, fired, msUntilFire };
}
