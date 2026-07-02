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
//
// Phase 4 (2026-06-01) — dual-mode. When the ``native_uploader``
// feature flag is ON at start() time, Transistorsoft owns the queue
// and POSTs directly; the JS side observes onHttp events to update
// stats + ring buffer, polls getCount() for the queue depth, and
// flushNow() becomes a thin wrapper around sync(). When OFF, the
// hook behaves exactly as Phase 2 — JS owns the queue + flush.

import NetInfo, { type NetInfoState } from "@react-native-community/netinfo";
import Constants from "expo-constants";
import { useCallback, useEffect, useRef, useState } from "react";

import { gpsPointToWire } from "@sailline/shared";

import { API_URL, apiFetch } from "../api";
import { postRecorderDebrief } from "../api/recorderDebrief";
import { auth } from "../firebase";
import {
  getNativeQueueCount,
  onNativeHttp,
  syncNow,
  type LocalPoint,
  startWatcher,
} from "./backgroundGeolocation";
import {
  buildDebrief,
  emptyLiveStats,
  gatherDeviceInfo,
  type LiveStats,
} from "./debrief";
import { startImuCapture, type ImuCaptureHandle } from "./imuRecorder";
import { getFlag } from "./featureFlags";
import { clearQueue, loadQueue, saveQueue } from "./queue";
import {
  appendEntry,
  clearLog,
  loadLog,
  type RecorderLogEntry,
} from "./recorderLog";
import {
  deriveUploadStatus,
  statsToStatusInputs,
  type UploadStatus,
} from "./uploadStatus";

const FLUSH_INTERVAL_MS = 30_000;
const FLUSH_GPS_BATCH_SIZE = 100;

/** How often we recompute the badge status. 1 s gives a snappy UI
 *  without thrashing — the underlying inputs only really change on
 *  flush completions and NetInfo events. */
const STATUS_RECOMPUTE_MS = 1_000;

type RecorderApi = {
  recording: boolean;
  error: string | null;
  points: LocalPoint[];
  queueLength: number;
  lastPoint: LocalPoint | null;
  /** Coarse upload health for the recording-screen badge. Always
   *  defined — defaults to "live" before the first event. */
  uploadStatus: UploadStatus;
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

  const gpsQueueRef = useRef<LocalPoint[]>([]); // unflushed points (JS mode)
  const watcherRef = useRef<{ stop: () => Promise<void> } | null>(null);
  const watcherPromiseRef = useRef<Promise<{
    stop: () => Promise<void>;
  }> | null>(null);
  const flushTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 2026-06-03 A3 fix — synchronous re-entry guard for start().
  //
  // The existing check `if (watcherRef.current || watcherPromiseRef.current)`
  // doesn't fire until AFTER the first async tick of start(); two
  // simultaneous callers (e.g., the auto-start timer firing while the
  // user also taps the Start FAB) can both pass the guard before
  // either has written to the refs. The second start() then calls
  // BackgroundGeolocation.ready() / .start() while the first is still
  // mid-flight, and Transistorsoft surfaces this as
  // "Waiting for previous start action to complete" via onError —
  // which the recording screen then renders as a red banner.
  //
  // startingRef is a plain boolean that flips true SYNCHRONOUSLY at the
  // very top of start() and clears in finally. That blocks the racing
  // second call before it can touch any Transistorsoft API.
  const startingRef = useRef(false);

  // Track whether we've seen at least one successful position fix since
  // the latest start(). Used to clear sticky start-time errors once the
  // recorder is demonstrably working — without this, a momentarily
  // surfaced "Waiting for previous start action…" stays on screen for
  // the rest of the session even after recording is fine.
  const haveLivePositionRef = useRef(false);

  // ── Phase 4 — native uploader state ─────────────────────────────────
  //
  // nativeModeRef is snapshotted at start() time so a mid-session
  // toggle of the feature flag doesn't half-switch the recorder.
  // Changing the flag mid-session is documented as taking effect on
  // the next start().
  //
  // nativeHttpSubRef holds the BackgroundGeolocation.onHttp
  // subscription so we can detach it on stop().
  //
  // nativeQueueIntervalRef polls the native queue depth every 5 s
  // while recording so the badge + queueLength stay roughly accurate
  // (the truth lives in Transistorsoft's SQLite).
  //
  // queueLengthRef is the unified ref read by the status-derivation
  // loop. JS mode updates it in onPosition; native mode updates it in
  // the poll + after onHttp success.
  const nativeModeRef = useRef<boolean>(false);
  // IMU capture handle (v1a, 2026-06-11). Lives alongside — never in —
  // the GPS pipeline: startImuCapture runs its own queue + flush and
  // POSTs IMU-only batches, so a sensor problem can't touch the track.
  const imuHandleRef = useRef<ImuCaptureHandle | null>(null);
  const nativeHttpSubRef = useRef<{ remove: () => void } | null>(null);
  const nativeQueueIntervalRef = useRef<ReturnType<typeof setInterval> | null>(
    null,
  );
  const queueLengthRef = useRef<number>(0);
  const flushingRef = useRef(false);
  // Tracks the last time saveQueue was called — used to debounce
  // AsyncStorage writes when the GPS queue grows large (offline periods).
  const lastQueueSaveRef = useRef<number>(0);
  // Stable ref to flushNow so the NetInfo effect can call it without
  // adding flushNow as a dependency (which would re-subscribe on every
  // render). Set after flushNow is defined below.
  const flushNowRef = useRef<(() => Promise<void>) | null>(null);
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

  // ── Phase 3 — upload-status badge state ─────────────────────────────
  //
  // netReachableRef tracks the latest NetInfo reading; isInternetReachable
  // can be null when the OS hasn't decided yet (cellular handoff, etc.).
  // null is treated as "online" by deriveUploadStatus — we don't flag
  // offline without evidence.
  //
  // uploadStatus is React state because the badge component subscribes
  // to it. A 1 s interval re-derives the status from refs + NetInfo
  // and only setState's if it actually changed; cheap and avoids the
  // badge re-rendering every second.
  const netReachableRef = useRef<boolean | null>(null);
  const [uploadStatus, setUploadStatus] = useState<UploadStatus>("live");

  // ── NetInfo subscription ────────────────────────────────────────────
  //
  // Mount once for the hook's lifetime — independent of recording
  // state because we want fresh data the moment the user opens the
  // recording screen, not 1 s after they press Start.
  //
  // Offline→online transition (2026-06-30): when service returns after
  // an offline period (common on Lake Michigan), immediately flush the
  // queue instead of waiting up to 30s for the next flush timer. This
  // drains accumulated GPS fixes as soon as cell returns, minimizing
  // the window where data only exists in AsyncStorage.
  const wasOfflineRef = useRef(false);
  useEffect(() => {
    const sub = NetInfo.addEventListener((state: NetInfoState) => {
      // ``isInternetReachable`` is what we want — ``isConnected`` is a
      // weaker signal (wifi associated but captive portal). NetInfo
      // reports null when reachability hasn't been probed yet.
      const reachable = state.isInternetReachable;
      netReachableRef.current = reachable;

      // Drain the queue when service returns after an offline period.
      // null (undetermined) is treated as "no change" — we only fire
      // on a definitive false→true transition.
      if (wasOfflineRef.current && reachable === true) {
        wasOfflineRef.current = false;
        // Small delay so NetInfo settles + the flushNow closure is fresh
        setTimeout(() => void flushNowRef.current?.(), 500);
      } else if (reachable === false) {
        wasOfflineRef.current = true;
      }
    });
    return () => sub();
  }, []);

  // ── Upload-status recompute loop ─────────────────────────────────────
  //
  // Cheap derivation off refs every second. We compare to the
  // currently-rendered status and only commit if it changed, so the
  // badge re-renders only on transitions.
  //
  // Gated on `recording` (2026-06-30): the badge isn't shown when not
  // recording, so the interval is pointless idle work — a 1 Hz timer
  // allocating objects + calling deriveUploadStatus for nothing.
  //
  // queueLengthRef is the authoritative source — JS mode writes it
  // from onPosition, native mode writes it from the poll + onHttp.
  useEffect(() => {
    if (!recording) return;
    const interval = setInterval(() => {
      const next = deriveUploadStatus(
        statsToStatusInputs(
          statsRef.current,
          queueLengthRef.current,
          netReachableRef.current,
        ),
        Date.now(),
      );
      setUploadStatus((prev) => (prev === next ? prev : next));
    }, STATUS_RECOMPUTE_MS);
    return () => clearInterval(interval);
  }, [recording]);

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

  // ── Native-mode HTTP event handler ───────────────────────────────────
  //
  // Fires whenever Transistorsoft's HTTP layer attempts a POST. Used
  // to drive LiveStats + the ring buffer in native mode. The handler
  // is registered in start() and removed in stop().
  const handleNativeHttp = useCallback(
    (event: { success: boolean; status: number; responseText?: string }) => {
      const now = Date.now();
      statsRef.current.attempts += 1;

      let insertedCount: number | undefined;
      if (event.success) {
        // Parse the TelemetryAck for the actually-inserted count.
        // Phase 1's ON CONFLICT means re-sent batches return 0; we
        // count what the server says landed, not what was sent.
        if (event.responseText) {
          try {
            const ack = JSON.parse(event.responseText) as TelemetryAck;
            if (typeof ack.gps_inserted === "number") {
              insertedCount = ack.gps_inserted;
            }
          } catch {
            // ignore — keep the success but no inserted count
          }
        }
        statsRef.current.successes += 1;
        if (insertedCount !== undefined) {
          statsRef.current.pointsUploaded += insertedCount;
        }
        if (statsRef.current.lastSuccessTs != null) {
          const gapS = (now - statsRef.current.lastSuccessTs) / 1000;
          if (gapS > statsRef.current.longestSuccessGapSeen) {
            statsRef.current.longestSuccessGapSeen = gapS;
          }
        }
        statsRef.current.lastSuccessTs = now;
      } else if (event.status >= 500) {
        statsRef.current.http5xx += 1;
      } else if (event.status >= 400) {
        statsRef.current.http4xx += 1;
      } else {
        // status 0 / -1 in Transistorsoft means network failure.
        statsRef.current.networkErrors += 1;
      }

      recordLog({
        kind: "flush",
        status: event.success ? "ok" : "error",
        http_status: event.status,
        inserted: insertedCount,
        queue_depth_after: queueLengthRef.current,
        message: event.success ? undefined : event.responseText?.slice(0, 200),
      });

      // Reconcile the badge queue depth eagerly on every event so the
      // UI matches the native queue without waiting for the 5 s poll.
      void getNativeQueueCount().then((n) => {
        queueLengthRef.current = n;
        setQueueLength(n);
      });
    },
    [recordLog],
  );

  // ── Flush ───────────────────────────────────────────────────────────
  const flushNow = useCallback(async () => {
    const id = raceIdRef.current;
    if (!id) return;

    // Native mode: tell Transistorsoft to drain its queue immediately.
    // The onHttp handler picks up the resulting events to update stats.
    if (nativeModeRef.current) {
      await syncNow();
      return;
    }

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

  // Keep the ref current so the NetInfo effect can call flushNow
  // without it being a dependency (avoids re-subscribing on every render).
  flushNowRef.current = flushNow;

  // ── Position handler ─────────────────────────────────────────────────
  //
  // Branches on mode. JS mode: push onto the local queue, persist,
  // flush at 100. Native mode: skip the queue (Transistorsoft owns
  // it) but keep the breadcrumb UI state and the capture counter
  // accurate. Native queueLength is reconciled by the poll + onHttp.
  const onPosition = useCallback(
    (point: LocalPoint) => {
      const id = raceIdRef.current;
      if (!id) return;

      // First successful fix of this session — clear any sticky start
      // error so a transient "Waiting for previous start action…" or
      // "permission probe" message doesn't haunt the screen for the
      // rest of the race once we're demonstrably capturing fixes. We
      // only do this on the first fix to avoid clobbering a fresh
      // error raised mid-session (e.g., GPS loss).
      if (!haveLivePositionRef.current) {
        haveLivePositionRef.current = true;
        setError(null);
      }

      // Breadcrumb UI — capped to prevent unbounded memory growth on long
      // races. A 3-hour race at 1 Hz = 10,800 points; without a cap this
      // array grows without limit and every setPoints triggers re-renders
      // in ActualRouteLayer + useNextMarkGuidance, creating an O(N²)
      // cascade that eventually crashes the app (root cause of the Silly
      // Race mid-race crashes). The cap only affects the UI trail; the
      // GPS queue (gpsQueueRef / Transistorsoft SQLite) is the source of
      // truth for upload and is NOT capped.
      const BREADCRUMB_CAP = 5000;
      setPoints((prev) => {
        const next = [...prev, point];
        return next.length > BREADCRUMB_CAP
          ? next.slice(next.length - BREADCRUMB_CAP)
          : next;
      });
      setLastPoint(point);

      // ── Stats: capture (same in both modes) ────────────────────
      statsRef.current.pointsCaptured += 1;
      if (statsRef.current.startedAt == null) {
        statsRef.current.startedAt = Date.now();
      }

      if (nativeModeRef.current) {
        // Native mode — queue depth comes from getNativeQueueCount(),
        // updated by the polling interval + onHttp success. We
        // optimistically bump the displayed queue here so the badge
        // reflects the just-captured fix without waiting for the poll.
        queueLengthRef.current = queueLengthRef.current + 1;
        setQueueLength(queueLengthRef.current);
        if (queueLengthRef.current > statsRef.current.maxQueueDepth) {
          statsRef.current.maxQueueDepth = queueLengthRef.current;
        }
        return;
      }

      // ── JS mode ────────────────────────────────────────────────
      gpsQueueRef.current.push(point);
      // Persist to AsyncStorage for crash recovery. When the queue is
      // small (< 50 points) we save on every fix so a crash loses at
      // most 1 second of data. When the queue grows (offline period,
      // upload failures), the JSON.stringify cost grows O(N) and we
      // debounce to every 5s — losing 5s of data on a crash is
      // acceptable, but the 1 Hz O(N) write was contributing to the
      // mid-race crash cascade on the Silly Race.
      const depth = gpsQueueRef.current.length;
      const shouldSave =
        depth < 50 || Date.now() - lastQueueSaveRef.current > 5000;
      if (shouldSave) {
        lastQueueSaveRef.current = Date.now();
        void saveQueue(id, gpsQueueRef.current);
      }
      queueLengthRef.current = depth;
      setQueueLength(depth);
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
    // 2026-06-03 A3 — synchronous re-entry guard. startingRef flips
    // BEFORE the first await so a racing second start() (e.g. the
    // auto-start timer firing while the user taps Start) is rejected
    // immediately, rather than slipping past the async refs and
    // calling Transistorsoft.start() twice. The original watcher refs
    // alone weren't enough: they're only set inside the promise body,
    // so both callers could clear the check.
    if (
      startingRef.current ||
      watcherRef.current ||
      watcherPromiseRef.current
    ) {
      return;
    }
    startingRef.current = true;
    haveLivePositionRef.current = false;

    setError(null);
    setRecording(true);

    // Fresh breadcrumb for this session. Points intentionally survive
    // stop() — the post-race Debrief screen (app/(app)/debrief/[id].tsx)
    // draws the just-sailed track straight from memory — so the reset
    // lives HERE, when the next recording begins, not in stop(). The
    // crash-recovery restore below re-populates from the persisted
    // queue when one exists.
    setPoints([]);
    setLastPoint(null);

    // ── Phase 2 — reset stats + restore log for this race ────────────
    statsRef.current = emptyLiveStats();
    logRef.current = await loadLog(id);

    // ── Phase 4 — snapshot the feature flag for this session ─────────
    //
    // Snapshotted here so a mid-session toggle doesn't half-switch the
    // recorder. Native mode needs a fresh Firebase token for the
    // initial native config; if we can't get one, fall back to JS to
    // avoid leaving the user with a recorder that captures but never
    // uploads.
    const wantNative = await getFlag("native_uploader");
    let nativeUploaderCfg: { url: string; authHeader: string } | undefined;
    if (wantNative) {
      try {
        const user = auth.currentUser;
        const token = user ? await user.getIdToken() : null;
        if (token) {
          nativeUploaderCfg = {
            url: `${API_URL}/api/races/${id}/telemetry`,
            authHeader: `Bearer ${token}`,
          };
        }
      } catch {
        // ignore — fall back to JS mode below
      }
    }
    nativeModeRef.current = nativeUploaderCfg !== undefined;
    recordLog({
      kind: "lifecycle",
      status: "info",
      message: `start mode=${nativeModeRef.current ? "native" : "js"}`,
    });

    // Restore any points left over from a previous (interrupted) JS-mode
    // session for this race so they flush with the new run. Skipped in
    // native mode: Transistorsoft's SQLite store already holds the
    // unsent fixes from the previous session and will drain them on
    // its own. (Mixed-mode restore is intentionally not supported —
    // switching modes between sessions resets the queue boundary.)
    if (!nativeModeRef.current) {
      const restored = await loadQueue(id);
      if (restored.length > 0) {
        gpsQueueRef.current = restored.slice();
        queueLengthRef.current = restored.length;
        setQueueLength(restored.length);
        setPoints(restored.slice());
        setLastPoint(restored[restored.length - 1]);
      }
    } else {
      // Native mode: read the current native queue depth so the badge
      // doesn't start at 0 if there's already a backlog from a prior
      // run.
      const initial = await getNativeQueueCount();
      queueLengthRef.current = initial;
      setQueueLength(initial);
    }

    watcherPromiseRef.current = startWatcher({
      onPosition,
      onError,
      nativeUploader: nativeUploaderCfg,
    })
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

    if (nativeModeRef.current) {
      // Subscribe to native HTTP events for stats + ring buffer.
      nativeHttpSubRef.current = onNativeHttp(handleNativeHttp);
      // Poll the native queue count every 5 s as a backstop in case
      // onHttp's reconcile fires slowly under heavy load. Cheap call;
      // foreground-only is fine — when JS sleeps the badge stops
      // updating anyway.
      nativeQueueIntervalRef.current = setInterval(() => {
        void getNativeQueueCount().then((n) => {
          queueLengthRef.current = n;
          setQueueLength(n);
        });
      }, 5_000);
    } else {
      // JS mode — the existing 30 s flush timer drives uploads.
      flushTimerRef.current = setInterval(
        () => void flushNow(),
        FLUSH_INTERVAL_MS,
      );
    }

    // ── IMU capture (v1a, 2026-06-11) — best-effort sidecar ──────────
    // Null on devices without a usable IMU. Failure here must never
    // affect GPS recording, hence the catch-all.
    try {
      imuHandleRef.current = await startImuCapture(id);
      recordLog({
        kind: "lifecycle",
        status: "info",
        message: `imu ${imuHandleRef.current ? "on" : "unavailable"}`,
      });
    } catch {
      imuHandleRef.current = null;
    }

    // 2026-06-03 A3 — clear the synchronous re-entry guard now that
    // start() is fully wired up. A later call may retry without being
    // rejected for "already starting." If the watcher promise rejected
    // above we still clear: stop()+start() is the documented recovery
    // path and shouldn't be blocked by a stale guard.
    startingRef.current = false;
  }, [onPosition, onError, flushNow, recordLog, handleNativeHttp]);

  const stop = useCallback(async () => {
    // Belt-and-braces: clear the re-entry guard so a subsequent start()
    // can never be blocked because a prior start() threw before its
    // clean-up line ran.
    startingRef.current = false;
    haveLivePositionRef.current = false;
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
    // ── IMU teardown (final flush inside stop) — best-effort ──────────
    if (imuHandleRef.current) {
      try {
        await imuHandleRef.current.stop();
      } catch {
        /* best effort */
      }
      imuHandleRef.current = null;
    }
    // ── Phase 4 cleanup — native subscriptions + poll ─────────────────
    if (nativeHttpSubRef.current) {
      nativeHttpSubRef.current.remove();
      nativeHttpSubRef.current = null;
    }
    if (nativeQueueIntervalRef.current) {
      clearInterval(nativeQueueIntervalRef.current);
      nativeQueueIntervalRef.current = null;
    }
    setRecording(false);
    // Final flush so the last few seconds ship without waiting 30 s.
    // Works in both modes: JS mode runs the apiFetch loop; native mode
    // calls Transistorsoft.sync().
    await flushNow();
    // If the queue drained clean, drop the persisted entry so the next
    // session starts empty. Only relevant in JS mode — native mode
    // never wrote to the AsyncStorage queue.
    const id = raceIdRef.current;
    if (!nativeModeRef.current && id && gpsQueueRef.current.length === 0) {
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
      if (imuHandleRef.current) {
        void imuHandleRef.current.stop();
        imuHandleRef.current = null;
      }
      if (flushTimerRef.current) {
        clearInterval(flushTimerRef.current);
        flushTimerRef.current = null;
      }
      if (nativeHttpSubRef.current) {
        nativeHttpSubRef.current.remove();
        nativeHttpSubRef.current = null;
      }
      if (nativeQueueIntervalRef.current) {
        clearInterval(nativeQueueIntervalRef.current);
        nativeQueueIntervalRef.current = null;
      }
    };
  }, []);

  return {
    recording,
    error,
    points,
    queueLength,
    lastPoint,
    uploadStatus,
    start,
    stop,
    flushNow,
  };
}
