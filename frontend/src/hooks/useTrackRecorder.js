// useTrackRecorder — continuous GPS capture for an active race.
//
// Lifecycle: caller flips `recording` on by calling `start()`. The hook
// asks the platform-adaptive geolocation adapter for a watcher
// (`createWatcher` in lib/geolocation.js — web watchPosition in the
// browser, Capacitor background-geolocation on Android), accumulates
// normalised points in an in-memory buffer, and flushes to
// `POST /api/races/{id}/track` every FLUSH_INTERVAL_MS or whenever the
// buffer reaches FLUSH_BATCH_SIZE, whichever comes first.
//
// Background tracking: in the web/PWA build, the OS pauses
// `watchPosition` when the tab is hidden or the screen locks — points
// are lost during those windows. In the Android Capacitor build the
// adapter switches to a foreground-service watcher that survives screen
// lock. The hook itself is unaware of which path is active; all
// platform branching lives in the adapter.
//
// Offline durability: every appended point is also persisted to a
// per-race localStorage key. Points stay there until the server has
// 201'd them. If the network drops mid-passage, points keep accumulating
// locally and drain on the next successful flush. If the user closes
// the tab and reopens before the race ends, the queue is restored on
// re-entry to the recorder — no points lost.
//
// Per-race scoping: localStorage key is `sailline.trackQueue.<raceId>`
// so multiple in-flight races never cross-contaminate.
//
// Returns: { recording, error, queueLength, lastPoint, points, start, stop, flushNow }
//   - `points` is the in-memory log of everything captured this session,
//     used by MapView to draw the green breadcrumb. Includes already-
//     flushed points so the trail stays visible.
//   - `queueLength` is the count of *unflushed* points (server hasn't
//     acked them yet) — useful for surfacing a "12 points pending" dot
//     when offline.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api";
import { createWatcher } from "../lib/geolocation";

const FLUSH_INTERVAL_MS = 30_000;
const FLUSH_BATCH_SIZE = 100;

const STORAGE_PREFIX = "sailline.trackQueue.";

function storageKey(raceId) {
  return `${STORAGE_PREFIX}${raceId}`;
}

function readQueue(raceId) {
  try {
    const raw = localStorage.getItem(storageKey(raceId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeQueue(raceId, queue) {
  try {
    localStorage.setItem(storageKey(raceId), JSON.stringify(queue));
  } catch {
    /* quota exceeded or storage disabled — best effort */
  }
}

function clearQueue(raceId) {
  try {
    localStorage.removeItem(storageKey(raceId));
  } catch {
    /* ignore */
  }
}

/**
 * @param {string|null} raceId  the race to record into. Null disables.
 * @returns recorder API
 */
export function useTrackRecorder(raceId) {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState(null);
  const [points, setPoints] = useState([]); // breadcrumb (everything captured)
  const [queueLength, setQueueLength] = useState(0);
  const [lastPoint, setLastPoint] = useState(null);

  // Refs hold the live values for callbacks that close over them. State
  // is just for re-render — refs are the source of truth.
  const queueRef = useRef([]);            // unflushed points
  const watcherHandleRef = useRef(null);  // { stop } from the adapter, once resolved
  const watcherPromiseRef = useRef(null); // Promise<handle> while setup is in flight
  const flushTimerRef = useRef(null);
  const flushingRef = useRef(false);
  const raceIdRef = useRef(raceId);
  raceIdRef.current = raceId;

  // ── Restore any pending queue when raceId becomes set ─────────────
  useEffect(() => {
    if (!raceId) {
      queueRef.current = [];
      setQueueLength(0);
      setPoints([]);
      setLastPoint(null);
      return;
    }
    const restored = readQueue(raceId);
    if (restored.length > 0) {
      queueRef.current = restored.slice();
      setQueueLength(restored.length);
      // Surface restored points on the breadcrumb so the trail picks up
      // where it left off after a tab reload.
      setPoints(restored.slice());
      setLastPoint(restored[restored.length - 1]);
    }
  }, [raceId]);

  // ── Flush ─────────────────────────────────────────────────────────
  const flushNow = useCallback(async () => {
    const id = raceIdRef.current;
    if (!id) return;
    if (flushingRef.current) return;
    if (queueRef.current.length === 0) return;

    flushingRef.current = true;
    // Snapshot what we're sending. New points captured during the
    // round trip stay in queueRef and ride the next flush.
    const batch = queueRef.current.slice();
    try {
      await apiFetch(`/api/races/${id}/track`, {
        method: "POST",
        body: { points: batch },
      });
      // Drop the acked points from the head of the queue. We compare by
      // the recorded_at boundary rather than just splicing the first N
      // because new points may have appended during the round trip.
      const acked = new Set(batch.map((p) => p.recorded_at));
      queueRef.current = queueRef.current.filter(
        (p) => !acked.has(p.recorded_at),
      );
      writeQueue(id, queueRef.current);
      setQueueLength(queueRef.current.length);
      setError(null);
    } catch (e) {
      // Network failure, 401 (token expired), 5xx — keep the queue.
      // Next interval (or the next captured point that pushes us past
      // FLUSH_BATCH_SIZE) retries.
      setError(e.message || String(e));
    } finally {
      flushingRef.current = false;
    }
  }, []);

  // ── Position handler — receives an already-normalised point ───────
  const onPosition = useCallback(
    (point) => {
      const id = raceIdRef.current;
      if (!id) return;

      queueRef.current.push(point);
      writeQueue(id, queueRef.current);
      setQueueLength(queueRef.current.length);

      setPoints((prev) => [...prev, point]);
      setLastPoint(point);

      if (queueRef.current.length >= FLUSH_BATCH_SIZE) {
        flushNow();
      }
    },
    [flushNow],
  );

  const onPositionError = useCallback((err) => {
    setError(err?.message || `geolocation error ${err?.code ?? ""}`);
  }, []);

  // ── Start / stop ──────────────────────────────────────────────────
  const start = useCallback(() => {
    if (!raceIdRef.current) {
      setError("No active race — set one before recording.");
      return;
    }
    // Idempotent — guard against double-start while a watcher is being
    // set up or is already live.
    if (watcherHandleRef.current || watcherPromiseRef.current) return;

    setError(null);
    setRecording(true);

    // Kick off the adapter. We store the promise so stop() can wait for
    // setup to finish if the user races start→stop too quickly.
    watcherPromiseRef.current = createWatcher({
      onPosition,
      onError: onPositionError,
    })
      .then((handle) => {
        watcherHandleRef.current = handle;
        watcherPromiseRef.current = null;
        return handle;
      })
      .catch((e) => {
        onPositionError(e);
        watcherPromiseRef.current = null;
      });

    flushTimerRef.current = setInterval(flushNow, FLUSH_INTERVAL_MS);
  }, [onPosition, onPositionError, flushNow]);

  const stop = useCallback(async () => {
    // Wait for any in-flight watcher setup, then tear it down. This
    // matters on the native path where addWatcher is async — calling
    // stop() before it resolves would otherwise orphan the watcher.
    if (watcherPromiseRef.current) {
      try {
        await watcherPromiseRef.current;
      } catch {
        /* setup already errored — onPositionError logged it */
      }
    }
    if (watcherHandleRef.current) {
      try {
        await watcherHandleRef.current.stop();
      } catch {
        /* best effort */
      }
      watcherHandleRef.current = null;
    }
    if (flushTimerRef.current) {
      clearInterval(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    setRecording(false);
    // Final flush so the user doesn't have to wait 30s for the last
    // few seconds of points to ship.
    await flushNow();
    // If the queue drained cleanly, drop the localStorage entry so a
    // fresh recording session starts empty.
    if (raceIdRef.current && queueRef.current.length === 0) {
      clearQueue(raceIdRef.current);
    }
  }, [flushNow]);

  // ── Drain on tab regain (covers "drop into airplane mode and back") ─
  useEffect(() => {
    const onOnline = () => {
      if (recording) flushNow();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible" && recording) flushNow();
    };
    window.addEventListener("online", onOnline);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("online", onOnline);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [recording, flushNow]);

  // ── Cleanup on unmount ───────────────────────────────────────────
  useEffect(() => {
    return () => {
      // Fire-and-forget; unmount can't await. The adapter's stop() is
      // best-effort anyway.
      if (watcherHandleRef.current) {
        watcherHandleRef.current.stop().catch(() => {});
        watcherHandleRef.current = null;
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
