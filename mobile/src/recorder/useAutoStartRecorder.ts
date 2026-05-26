// useAutoStartRecorder.ts — port of frontend/src/hooks/useAutoStartRecorder.js.
//
// Auto-fires recorder.start() 5 minutes before race.start_at. Pure
// JS/RN — no DOM, no browser APIs beyond setTimeout/Date.now, which RN
// provides identically.
//
// Differences from the web version:
//   - TypeScript signature.
//   - That's it. Logic is byte-identical so behaviour matches what the
//     web app does. A future shared-package extraction (packages/shared/
//     src/useAutoStartRecorder.js) would let both consumers import the
//     same module; for tonight the duplication is intentional — keeps
//     the web app's tests untouched.
//
// IMPORTANT behavioural caveat carried from the web: setTimeout in RN
// continues to fire while the app is in the background (unlike browsers
// throttling hidden tabs), BUT only while the JS runtime is alive. The
// OS can suspend the app; on Android we mitigate via Transistorsoft's
// foreground service once recording is active, but BEFORE start fires
// the JS runtime can be killed by the OS. So in practice this works
// reliably when the user has opened the app within the last ~15 minutes
// before gun time. Treat as a convenience, not a guarantee.

import { useEffect, useRef, useState } from "react";

const ARM_OFFSET_MS = 5 * 60 * 1000; // 5 minutes
const ARM_WINDOW_MS = 5 * 60 * 1000; // don't retro-fire if >5min past start

type Args = {
  raceId: string | null;
  startAtIso: string | null;
  enabled: boolean;
  recording: boolean;
  start: () => void | Promise<void>;
};

type Result = {
  armed: boolean;
  fired: boolean;
  msUntilFire: number | null;
};

export function useAutoStartRecorder({
  raceId,
  startAtIso,
  enabled,
  recording,
  start,
}: Args): Result {
  const [armed, setArmed] = useState(false);
  const [fired, setFired] = useState(false);
  const [msUntilFire, setMsUntilFire] = useState<number | null>(null);

  // Refs so the timer callback observes the latest values without
  // restarting the effect on every render.
  const recordingRef = useRef(recording);
  recordingRef.current = recording;
  const startRef = useRef(start);
  startRef.current = start;

  // Reset `fired` when the identifying key changes (new race or
  // re-scheduled start). Without this, editing start_at to a later time
  // after we already fired would prevent the re-arm from acting.
  const keyRef = useRef<string | null>(null);

  useEffect(() => {
    const key =
      enabled && raceId && startAtIso ? `${raceId}|${startAtIso}` : null;
    if (key !== keyRef.current) {
      keyRef.current = key;
      setFired(false);
    }

    setArmed(false);
    setMsUntilFire(null);

    if (!key || !startAtIso) return;

    const startAt = new Date(startAtIso).getTime();
    if (Number.isNaN(startAt)) return;

    const armAt = startAt - ARM_OFFSET_MS;
    const now = Date.now();
    const delay = armAt - now;

    if (delay <= 0) {
      const sincePast = -delay;
      if (sincePast > ARM_OFFSET_MS + ARM_WINDOW_MS) {
        // Race started long ago — don't auto-start.
        return;
      }
      const t = setTimeout(() => {
        if (!recordingRef.current) {
          try {
            void startRef.current?.();
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
          void startRef.current?.();
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
