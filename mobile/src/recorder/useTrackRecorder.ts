// useTrackRecorder.ts — RN background GPS recorder.
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
//
// Phase 2 (2026-06-01) — recorder diagnostics. The hook now maintains
// a LiveStats tally and an on-device ring buffer (recorderLog.ts)
// recording every flush attempt. On stop() the hook builds and POSTs
// a RecorderDebrief blob (best-effort) so the next failure is
// diagnosable without trawling Cloud Logging. See
// sailline-docs/2026-06-01_durable-upload-pipeline-plan.md.

import Constants from "expo-constants";
import { useCallback, useEffect, useRef, useState } from "react";

import { gpsPointToWire } from "@sailline/shared";

import { apiFetch } from "../api";
import { postRecorderDebrief } from "../api/recorderDebrief";
import {
  type LocalPoint,
  startWatcher,
} from "./backgroundGeolocation";
import {
  buildDebrief,
  emptyLiveStats,
  gatherDeviceInfo,
  type LiveStats,
} from "./debrief";
import { clearQueue, loadQueue, saveQueue } from "./queue";
import {
  appendEntry,
  clearLog,
  loadLog,
  type RecorderLogEntry,
} from "./recorderLog";

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

/** Server ack from POST /telemetry. Used to count actually-inserted
 *  points (the Phase 1 idempotency contract reports landed-rows,
 *  not sent-rows). */
type TelemetryAck = {
  gps_inserted?: number;
  imu_inserted?: number;
  calibration_inserted?: boolean;
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

  // ── Phase 2 — diagnostics state ────────────────────────────────────
  //
  // statsRef accumulates upload/capture counters across the session.
  // logRef is the in-memory mirror of the AsyncStorage ring buffer
  // (recorderLog.ts). Both are refs (not state) because they update
  // on the hot path of every flush; re-rendering on each change would
  // be noisy and unnecessary — the debug screen reloads from
  // AsyncStorage rather than subscribing to state.
  const statsRef = useRef<LiveStats>(emptyLiveStats());
  const logRef = useRef<RecorderLogEntry[]>([]);

  /**
   * Append to the ring buffer, updating both the in-memory mirror and
   * AsyncStorage. Fire-and-forget; errors are swallowed inside
   * recorderLog.appendEntry.
   */
  const recordLog = useCallback(
    (entry: Partial<RecorderLogEntry>) => {
      const id = raceIdRef.current;
      if (!id) return;
      void appendEntry(id, logRef.current, entry).then((next) => {
        logRef.current = next;
      });
    },
    [],
  );

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

    statsRef.current.attempts += 1;
    const attemptStart = Date.now();

    try {
      const ack = (await apiFetch<TelemetryAck>(
        `/api/races/${id}/telemetry`,
        {
          method: "POST",
          body,
        },
      )) ?? {};
      // Drop acked rows by their recorded_at stamp.
      const acked = new Set(chunk.map((p) => p.recorded_at));
      gpsQueueRef.current = gpsQueueRef.current.filter(
        (p) => !acked.has(p.recorded_at),
      );
      await saveQueue(id, gpsQueueRef.current);
      setQueueLength(gpsQueueRef.current.length);
      setError(null);

      // ── Stats: successful flush ─────────────────────────────────
      const insertedCount =
        typeof ack.gps_inserted === "number" ? ack.gps_inserted : chunk.length;
      statsRef.current.successes += 1;
      statsRef.current.pointsUploaded += insertedCount;
      const now = Date.now();
      if (statsRef.current.lastSuccessTs != null) {
        const gapS = (now - statsRef.current.lastSuccessTs) / 1000;
        if (gapS > statsRef.current.longestSuccessGapSeen) {
          statsRef.current.longestSuccessGapSeen = gapS;
        }
      }
      statsRef.current.lastSuccessTs = now;

      recordLog({
        kind: "flush",
        status: "ok",
        http_status: 200,
        duration_ms: now - attemptStart,
        batch_size: chunk.length,
        inserted: insertedCount,
        queue_depth_after: gpsQueueRef.current.length,
      });
    } catch (e) {
      // Network failure, 401 (token expired), 5xx — keep the queue.
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);

      // ── Stats: failed flush ─────────────────────────────────────
      // apiFetch throws errors of the form "API <status>: <text>" on
      // non-2xx and a plain Error("Not authenticated") or fetch error
      // otherwise. Parse the status when present; categorize as
      // network error when not.
      const statusMatch = /^API (\d{3}):/.exec(msg);
      const httpStatus = statusMatch ? Number(statusMatch[1]) : undefined;
      if (httpStatus !== undefined && httpStatus >= 500) {
        statsRef.current.http5xx += 1;
      } else if (httpStatus !== undefined && httpStatus >= 400) {
        statsRef.current.http4xx += 1;
      } else {
        statsRef.current.networkErrors += 1;
      }

      recordLog({
        kind: "flush",
        status: "error",
        http_status: httpStatus,
        duration_ms: Date.now() - attemptStart,
        batch_size: chunk.length,
        queue_depth_after: gpsQueueRef.current.length,
        message: msg,
      });
    } finally {
      flushingRef.current = false;
    }
  }, [recordLog]);

  // ── Position handler ─────────────────────────────────────────────────
  const onPosition = useCallback(
    (point: LocalPoint) => {
      const id = raceIdRef.current;
      if (!id) return;
      gpsQueueRef.current.push(point);
      // Persist on every fix (1 Hz) so a crash/kill loses nothing.
      void saveQueue(id, gpsQueueRef.current);
      const depth = gpsQueueRef.current.length;
      setQueueLength(depth);
      setPoints((prev) => [...prev, point]);
      setLastPoint(point);

      // ── Stats: capture ──────────────────────────────────────────
      statsRef.current.pointsCaptured += 1;
      if (statsRef.current.startedAt == null) {
        statsRef.current.startedAt = Date.now();
      }
      if (depth > statsRef.current.maxQueueDepth) {
        statsRef.current.maxQueueDepth = depth;
      }

      if (depth >= FLUSH_GPS_BATCH_SIZE) {
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

    // ── Phase 2 — reset stats + restore log for this race ────────────
    statsRef.current = emptyLiveStats();
    logRef.current = await loadLog(id);
    recordLog({ kind: "lifecycle", status: "info", message: "start" });

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
  }, [onPosition, onError, flushNow, recordLog]);

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

    // ── Phase 2 — post the debrief, best-effort ─────────────────────
    //
    // We do this AFTER the final flush so points_uploaded reflects the
    // very-last batch. The debrief itself is diagnostic, not load-
    // bearing: a failure to POST it does not surface to the UI, does
    // not block teardown, and does not clear the on-device log (so a
    // later retry from the debug screen could resend if we add one).
    if (id) {
      recordLog({ kind: "lifecycle", status: "info", message: "stop" });
      try {
        const expoExtra =
          (Constants.expoConfig?.extra ?? {}) as Record<string, unknown>;
        const buildId =
          typeof expoExtra.eas === "object" && expoExtra.eas != null
            ? (expoExtra.eas as { buildId?: string }).buildId
            : undefined;
        const debrief = buildDebrief({
          stats: statsRef.current,
          log: logRef.current,
          queueDepthAtStop: gpsQueueRef.current.length,
          endTsMs: Date.now(),
          device: gatherDeviceInfo({
            app_version: Constants.expoConfig?.version,
            build_id: buildId,
          }),
        });
        await postRecorderDebrief(id, debrief);
        // Drop the on-device log only if the debrief landed — keeps
        // the next debug-screen view clean. If it didn't land the log
        // stays around for inspection on the next foreground.
        await clearLog(id);
      } catch (e) {
        // Best-effort: surface to the on-device log only.
        recordLog({
          kind: "error",
          status: "error",
          message: e instanceof Error ? e.message : String(e),
        });
      }
    }
  }, [flushNow, recordLog]);

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
