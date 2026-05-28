// useAutoStartRecorder.ts — three-tier auto-start orchestration.
//
// Layered triggers, all calling the same recorder.start() — which guards
// on recordingRef.current so only the first one wins:
//
//   1. Foreground setTimeout (this hook) — most precise. Fires at the
//      exact instant the JS runtime decides. Active whenever the JS
//      runtime is alive (foreground OR background while not suspended).
//   2. T-6 local notification (scheduledAutoStart) — visible reminder
//      delivered by the OS regardless of JS state. Tapping it wakes the
//      app and calls start() via the onFire ref.
//   3. T-5 BackgroundFetch.scheduleTask (scheduledAutoStart) — silent OS
//      callback at T-5 min. If JS is alive, calls start() directly. If
//      JS is dead (cold-killed app), posts a fallback "race starting"
//      notification (no headless recorder start — too much state to
//      bootstrap from cold).
//
// IMPORTANT behavioural caveat: iOS BackgroundFetch is best-effort. The
// T-5 task may fire late by up to a few minutes in Low Power Mode or
// when the app hasn't been opened in a while. The T-6 notification is
// the user-visible safety net for that case.
//
// Differences from the original web version:
//   - TypeScript signature.
//   - Adds OS-level fallbacks (notification + BG fetch) alongside the
//     in-process setTimeout. The setTimeout is still the fast path when
//     the app is open; the fallbacks cover the suspended-app case.
//   - On mount, replays any pending notification tap that happened
//     before the hook had registered its onFire ref (cold-start path).

import { useEffect, useRef, useState } from "react";

import {
  cancelAutoStart,
  replayPendingTap,
  scheduleAutoStart,
  setOnFire,
} from "./scheduledAutoStart";

const ARM_OFFSET_MS = 5 * 60 * 1000; // 5 minutes
const ARM_WINDOW_MS = 5 * 60 * 1000; // don't retro-fire if >5min past start

// Module-scoped "we've already fired this auto-start" memory.
//
// WHY this lives outside the hook: useState `fired` resets on every mount,
// and the hook lives in (app)/index.tsx which RE-MOUNTS whenever the user
// navigates back from /recording → /. Without persistence, the sequence
// "tap Stop → router.replace('/') → hook remounts → delay is still <= 0
// because the planned start was minutes ago → setTimeout(fire, 0) → start()
// → useEffect detects recording=true → router.replace('/recording')"
// becomes an inescapable loop. Module scope survives navigation; the only
// thing that should clear an entry is the user editing the race's start_at
// (handled in the effect below — different key → cleared).
const firedKeys: Set<string> = new Set();

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

  // ── Register the onFire callback for OS-level triggers ──────────────
  //
  // Wraps start() with the same recording guard the setTimeout uses, so
  // notification-tap and BG-fetch firings are also idempotent. Re-runs
  // every render so the closure stays current with the latest start.
  useEffect(() => {
    setOnFire(async () => {
      if (recordingRef.current) return;
      try {
        await startRef.current?.();
      } catch {
        /* swallow */
      }
      // Mirror the foreground path's UI state update AND mark the
      // currently-armed key as fired so a later remount doesn't re-fire.
      const key = keyRef.current;
      if (key) firedKeys.add(key);
      setFired(true);
      setArmed(false);
      setMsUntilFire(null);
    });
    return () => setOnFire(null);
  }, []);

  // ── Replay pending cold-start taps ──────────────────────────────────
  //
  // If the user tapped the T-6 notification while the app was cold, the
  // OS launched the app but the tap response arrived before we had a
  // chance to register onFire. expo-notifications buffers the last
  // response — replay it here on first mount.
  useEffect(() => {
    void replayPendingTap();
  }, []);

  // ── Foreground setTimeout + OS-level fallback scheduling ────────────
  useEffect(() => {
    const key =
      enabled && raceId && startAtIso ? `${raceId}|${startAtIso}` : null;
    if (key !== keyRef.current) {
      // Key change = different race OR user edited start_at. Either way
      // the prior "we already fired" memory no longer applies — clear it
      // so the new (race, time) pair can arm and fire fresh.
      if (keyRef.current) firedKeys.delete(keyRef.current);
      keyRef.current = key;
      setFired(false);
    }

    setArmed(false);
    setMsUntilFire(null);

    if (!key || !startAtIso || !raceId) {
      // Disabled or no race — make sure any prior OS-level schedule for
      // the previous race is cleared (otherwise switching races would
      // leak a stale T-5 task).
      if (raceId) void cancelAutoStart(raceId);
      return;
    }

    // Module-scope guard: if this exact (race, startAt) pair already
    // triggered in this app session, don't re-arm. Survives the
    // navigation-driven remount that follows a user-initiated Stop.
    if (firedKeys.has(key)) {
      setFired(true);
      return;
    }

    const startAt = new Date(startAtIso).getTime();
    if (Number.isNaN(startAt)) return;

    const armAt = startAt - ARM_OFFSET_MS;
    const now = Date.now();
    const delay = armAt - now;

    // Always (re)schedule the OS-level fallbacks. scheduleAutoStart is
    // idempotent — it cancels prior schedules for this raceId before
    // creating new ones.
    void scheduleAutoStart(raceId, startAtIso);

    if (delay <= 0) {
      const sincePast = -delay;
      if (sincePast > ARM_OFFSET_MS + ARM_WINDOW_MS) {
        // Race started long ago — don't auto-start. Also cancel the
        // OS-level fallbacks (no value firing now).
        void cancelAutoStart(raceId);
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
        firedKeys.add(key);
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
      firedKeys.add(key);
      setFired(true);
      setArmed(false);
      setMsUntilFire(null);
    }, delay);

    return () => {
      clearTimeout(t);
      // Note: we deliberately do NOT cancelAutoStart on every cleanup,
      // because the cleanup also runs when the effect re-runs with the
      // same key. scheduleAutoStart at the top of the next run is
      // idempotent (it cancels then re-schedules), so leaving the
      // schedule in place between renders is safe and avoids a brief
      // window where the fallback is unscheduled.
    };
  }, [raceId, startAtIso, enabled]);

  return { armed, fired, msUntilFire };
}
