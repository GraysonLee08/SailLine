// useTrackRecorder.ts — RN background GPS recorder (Phase 1).
//
// The mobile port of frontend/src/hooks/useTrackRecorder.js, scoped to
// GPS only (IMU/heel + calibration are Phase 4). It reuses the proven
// queue/flush/drop-on-ack contract and the shared `gpsPointToWire`
// serializer, swapping the web platform glue for Transistorsoft capture
// (backgroundGeolocation.ts) and AsyncStorage durability (queue.ts).
//
// Flush policy matches the web recorder and the backend's batch caps:
//   - flush every 30 s, OR immediately when the queue hits 100 points
//   - each flush sends at most 100 GPS samples (backend rejects >100)
//   - drop acked rows on 200; keep them on any non-200 so an offline
//     burst drains on the next success
//
// There is no Wake Lock here by design: the whole point of the pivot is
// to capture with the screen OFF, which Transistorsoft's foreground
// service handles natively.

import { useCallback, useEffect, useRef, useState } from "react";

import { gpsPointToWire } from "@sailline/shared";

import { apiFetch } from "../api";
import {
  type LocalPoint,
  startWatcher,
} from "./backgroundGeolocation";
import { clearQueue, loadQueue, saveQueue } from "./queue";

const FLUSH_INTERVAL_MS = 30_000;
const FLUSH_GPS_BATCH_SIZE = 100;

type RecorderApi = {
  recording: boolean;
  error: string | null;
  points: LocalPoint[];
  queueLength: number;
  lastPoint: LocalPoint | null;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  flushNow: () => Promise<void>;
};

export function useTrackRecorder(raceId: string | null): RecorderApi {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [points, setPoints] = useState<LocalPoint[]>([]); // breadcrumb
  const [queueLength, setQueueLength] = useState(0);
  const [lastPoint, setLastPoint] = useState<LocalPoint | null>(null);

  const gpsQueueRef = useRef<LocalPoint[]>([]); // unflushed points
  const watcherRef = useRef<{ stop: () => Promise<void> } | null>(null);
  const watcherPromiseRef = useRef<Promise<{
    stop: () => Promise<void>;
  }> | null>(null);
  const flushTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const flushingRef = useRef(false);
  const raceIdRef = useRef<string | null>(raceId);
  raceIdRef.current = raceId;

  // ── Flush ───────────────────────────────────────────────────────────
  const flushNow = useCallback(async () => {
    const id = raceIdRef.current;
    if (!id) return;
    if (flushingRef.current) return;
    if (gpsQueueRef.current.length === 0) return;

    flushingRef.current = true;
    // Snapshot ≤100; points captured during the round trip ride the next
    // flush.
    const chunk = gpsQueueRef.current.slice(0, FLUSH_GPS_BATCH_SIZE);
    const body = { gps: chunk.map(gpsPointToWire) };

    try {
      await apiFetch(`/api/races/${id}/telemetry`, {
        method: "POST",
        body,
      });
      // Drop acked rows by their recorded_at stamp.
      const acked = new Set(chunk.map((p) => p.recorded_at));
      gpsQueueRef.current = gpsQueueRef.current.filter(
        (p) => !acked.has(p.recorded_at),
      );
      await saveQueue(id, gpsQueueRef.current);
      setQueueLength(gpsQueueRef.current.length);
      setError(null);
    } catch (e) {
      // Network failure, 401 (token expired), 5xx — keep the queue.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      flushingRef.current = false;
    }
  }, []);

  // ── Position handler ─────────────────────────────────────────────────
  const onPosition = useCallback(
    (point: LocalPoint) => {
      const id = raceIdRef.current;
      if (!id) return;
      gpsQueueRef.current.push(point);
      // Persist on every fix (1 Hz) so a crash/kill loses nothing.
      void saveQueue(id, gpsQueueRef.current);
      setQueueLength(gpsQueueRef.current.length);
      setPoints((prev) => [...prev, point]);
      setLastPoint(point);
      if (gpsQueueRef.current.length >= FLUSH_GPS_BATCH_SIZE) {
        void flushNow();
      }
    },
    [flushNow],
  );

  const onError = useCallback((err: Error) => {
    setError(err.message);
  }, []);

  // ── Start / stop ─────────────────────────────────────────────────────
  const start = useCallback(async () => {
    const id = raceIdRef.current;
    if (!id) {
      setError("No active race — set one before recording.");
      return;
    }
    // Idempotent — guard against double-start.
    if (watcherRef.current || watcherPromiseRef.current) return;

    setError(null);
    setRecording(true);

    // Restore any points left over from a previous (interrupted) session
    // for this race so they flush with the new run.
    const restored = await loadQueue(id);
    if (restored.length > 0) {
      gpsQueueRef.current = restored.slice();
      setQueueLength(restored.length);
      setPoints(restored.slice());
      setLastPoint(restored[restored.length - 1]);
    }

    watcherPromiseRef.current = startWatcher({ onPosition, onError })
      .then((handle) => {
        watcherRef.current = handle;
        watcherPromiseRef.current = null;
        return handle;
      })
      .catch((e) => {
        onError(e instanceof Error ? e : new Error(String(e)));
        watcherPromiseRef.current = null;
        throw e;
      });

    try {
      await watcherPromiseRef.current;
    } catch {
      // onError already surfaced it; leave recording=true so the user
      // sees the error state and can retry stop/start.
    }

    flushTimerRef.current = setInterval(() => void flushNow(), FLUSH_INTERVAL_MS);
  }, [onPosition, onError, flushNow]);

  const stop = useCallback(async () => {
    // Wait for any in-flight setup, then tear down the watcher.
    if (watcherPromiseRef.current) {
      try {
        await watcherPromiseRef.current;
      } catch {
        /* setup already errored */
      }
    }
    if (watcherRef.current) {
      try {
        await watcherRef.current.stop();
      } catch {
        /* best effort */
      }
      watcherRef.current = null;
    }
    if (flushTimerRef.current) {
      clearInterval(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    setRecording(false);
    // Final flush so the last few seconds ship without waiting 30 s.
    await flushNow();
    // If the queue drained clean, drop the persisted entry so the next
    // session starts empty.
    const id = raceIdRef.current;
    if (id && gpsQueueRef.current.length === 0) {
      await clearQueue(id);
    }
  }, [flushNow]);

  // ── Cleanup on unmount ───────────────────────────────────────────────
  useEffect(() => {
    return () => {
      if (watcherRef.current) {
        void watcherRef.current.stop();
        watcherRef.current = null;
      }
      if (flushTimerRef.current) {
        clearInterval(flushTimerRef.current);
        flushTimerRef.current = null;
      }
    };
  }, []);

  return {
    recording,
    error,
    points,
    queueLength,
    lastPoint,
    start,
    stop,
    flushNow,
  };
}
