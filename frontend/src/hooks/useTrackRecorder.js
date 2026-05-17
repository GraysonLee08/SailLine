// useTrackRecorder — continuous race telemetry capture (GPS + IMU + calibration).
//
// Lifecycle: caller flips `recording` on by calling `start()`. The hook:
//
//   1. Requests Screen Wake Lock (best effort) so the screen doesn't
//      time out while the page is foregrounded. Reacquired automatically
//      on visibilitychange. Released on stop().
//   2. Asks the platform-adaptive geolocation adapter for a watcher
//      (`createWatcher` in lib/geolocation.js — web watchPosition in the
//      browser, Capacitor background-geolocation on Android).
//   3. Prompts for DeviceOrientation permission on iOS (Android grants
//      automatically). If denied, recording continues GPS-only.
//   4. Starts a 10 Hz IMU sampler that reads the cached
//      DeviceOrientationEvent and queues `{t, heel_deg, pitch_deg, yaw_deg}`.
//
// On a regular interval (30 s) or when the GPS buffer reaches 100
// points, the hook flushes a single batch to
// `POST /api/races/{id}/telemetry`:
//
//     {
//       gps:         [{t, lat, lon, sog_kts, cog_deg, gps_acc_m}, ...],
//       imu:         [{t, heel_deg, pitch_deg, yaw_deg}, ...],
//       calibration: {captured_at, heel_zero_offset_deg, pitch_zero_offset_deg}?
//     }
//
// Calibration is queued by the UI via the hook's `captureCalibration()`
// method (only allowed while pre-start) and rides the next flush. Once
// acked, it's cleared from local state.
//
// Background tracking: in the web/PWA build, the OS pauses
// `watchPosition` when the tab is hidden or the screen locks — points
// are lost during those windows. The Wake Lock kept the screen on while
// we were visible; once a real lock happens, the Capacitor APK is the
// only robust answer. In the Android Capacitor build the adapter
// switches to a foreground-service watcher that survives screen lock.
// The hook itself is unaware of which path is active.
//
// Offline durability: every captured GPS/IMU sample is also persisted
// to a per-race localStorage key. Points stay there until the server
// has 200'd the batch they were in. If the network drops mid-passage,
// samples keep accumulating locally and drain on the next successful
// flush. If the user closes the tab and reopens before the race ends,
// the queue is restored on re-entry — no points lost.
//
// Per-race scoping: localStorage keys are `sailline.trackQueue.<raceId>`
// (GPS), `sailline.imuQueue.<raceId>` (IMU), and
// `sailline.calibration.<raceId>` (pending calibration). Multiple
// in-flight races never cross-contaminate.
//
// Returns: { recording, error, queueLength, lastPoint, points,
//            start, stop, flushNow, captureCalibration,
//            pendingCalibration, orientationPermission }
//   - `points` is the in-memory log used by MapView to draw the green
//     breadcrumb. Includes already-flushed points so the trail stays
//     visible.
//   - `queueLength` is the count of *unflushed* GPS points. IMU rows
//     drain quietly with the same flushes.
//   - `pendingCalibration` is the queued zero-offset awaiting its first
//     ack. UI uses it to confirm the Zero tap took.
//   - `orientationPermission` reflects the iOS prompt result and is
//     "not-needed" / "granted" / "denied" / "unsupported" / null.

import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "../api";
import { createWatcher } from "../lib/geolocation";
import {
  DEFAULT_PHONE_AXIS,
  remapEulerToBoat,
} from "../lib/imuAxes";
import {
  latest as latestOrientation,
  needsPermissionPrompt as orientationNeedsPrompt,
  requestPermission as requestOrientationPermission,
  start as startOrientationListener,
} from "../sensors/orientation";

const FLUSH_INTERVAL_MS = 30_000;
const FLUSH_GPS_BATCH_SIZE = 100;
const IMU_SAMPLE_HZ = 10;

const STORAGE_PREFIX_GPS = "sailline.trackQueue.";
const STORAGE_PREFIX_IMU = "sailline.imuQueue.";
const STORAGE_PREFIX_CAL = "sailline.calibration.";

function gpsKey(raceId) {
  return `${STORAGE_PREFIX_GPS}${raceId}`;
}
function imuKey(raceId) {
  return `${STORAGE_PREFIX_IMU}${raceId}`;
}
function calKey(raceId) {
  return `${STORAGE_PREFIX_CAL}${raceId}`;
}

function readJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    return parsed ?? fallback;
  } catch {
    return fallback;
  }
}

function writeJSON(key, value) {
  try {
    if (value == null) {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, JSON.stringify(value));
    }
  } catch {
    /* quota exceeded or storage disabled — best effort */
  }
}

function clearKey(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

/**
 * Translate the recorder's local point shape (used by the breadcrumb +
 * offline queue) into the `/telemetry` GPS wire shape. We keep the two
 * shapes separate so the breadcrumb logic stays decoupled from the API
 * contract (per the Session E plan).
 */
function gpsPointToWire(point) {
  return {
    t: point.recorded_at,
    lat: point.lat,
    lon: point.lon,
    sog_kts: Number.isFinite(point.speed_kts) ? point.speed_kts : null,
    cog_deg:
      Number.isFinite(point.heading_deg) && point.heading_deg >= 0
        ? point.heading_deg
        : null,
    gps_acc_m: Number.isFinite(point.gps_acc_m) ? point.gps_acc_m : null,
  };
}

/**
 * @param {string|null} raceId  the race to record into. Null disables.
 * @param {object} [opts]
 * @param {string} [opts.phoneAxis]  "fore-aft" | "port-stbd"
 * @returns recorder API
 */
export function useTrackRecorder(raceId, opts = {}) {
  const phoneAxis = opts.phoneAxis || DEFAULT_PHONE_AXIS;

  const [recording, setRecording] = useState(false);
  const [error, setError] = useState(null);
  const [points, setPoints] = useState([]); // breadcrumb (everything captured)
  const [queueLength, setQueueLength] = useState(0);
  const [lastPoint, setLastPoint] = useState(null);
  const [pendingCalibration, setPendingCalibration] = useState(null);
  const [orientationPermission, setOrientationPermission] = useState(null);

  // Refs hold the live values for callbacks that close over them.
  const gpsQueueRef = useRef([]);         // unflushed GPS points (local shape)
  const imuQueueRef = useRef([]);         // unflushed IMU samples (wire shape)
  const pendingCalibrationRef = useRef(null);
  const watcherHandleRef = useRef(null);  // { stop } from the adapter, once resolved
  const watcherPromiseRef = useRef(null); // Promise<handle> while setup is in flight
  const orientationHandleRef = useRef(null); // { stop } from the orientation listener
  const imuIntervalRef = useRef(null);
  const flushTimerRef = useRef(null);
  const flushingRef = useRef(false);
  const wakeLockRef = useRef(null);
  const raceIdRef = useRef(raceId);
  raceIdRef.current = raceId;
  const phoneAxisRef = useRef(phoneAxis);
  phoneAxisRef.current = phoneAxis;

  // ── Restore any pending queues when raceId becomes set ────────────
  useEffect(() => {
    if (!raceId) {
      gpsQueueRef.current = [];
      imuQueueRef.current = [];
      pendingCalibrationRef.current = null;
      setPendingCalibration(null);
      setQueueLength(0);
      setPoints([]);
      setLastPoint(null);
      return;
    }
    const restoredGps = readJSON(gpsKey(raceId), []);
    const restoredImu = readJSON(imuKey(raceId), []);
    const restoredCal = readJSON(calKey(raceId), null);
    if (Array.isArray(restoredGps) && restoredGps.length > 0) {
      gpsQueueRef.current = restoredGps.slice();
      setQueueLength(restoredGps.length);
      setPoints(restoredGps.slice());
      setLastPoint(restoredGps[restoredGps.length - 1]);
    }
    if (Array.isArray(restoredImu) && restoredImu.length > 0) {
      imuQueueRef.current = restoredImu.slice();
    }
    if (restoredCal && typeof restoredCal === "object") {
      pendingCalibrationRef.current = restoredCal;
      setPendingCalibration(restoredCal);
    }
  }, [raceId]);

  // ── Wake Lock helpers ─────────────────────────────────────────────
  const acquireWakeLock = useCallback(async () => {
    if (typeof navigator === "undefined" || !navigator.wakeLock) return;
    if (wakeLockRef.current) return;
    try {
      const sentinel = await navigator.wakeLock.request("screen");
      wakeLockRef.current = sentinel;
      // Browser auto-releases when the tab is hidden; we reacquire on
      // visibilitychange below. The 'release' event lets us null our ref
      // so the next reacquire works.
      sentinel.addEventListener("release", () => {
        if (wakeLockRef.current === sentinel) {
          wakeLockRef.current = null;
        }
      });
    } catch {
      /* permission denied or unsupported — best effort */
    }
  }, []);

  const releaseWakeLock = useCallback(async () => {
    const sentinel = wakeLockRef.current;
    wakeLockRef.current = null;
    if (sentinel) {
      try {
        await sentinel.release();
      } catch {
        /* best effort */
      }
    }
  }, []);

  // ── Flush ─────────────────────────────────────────────────────────
  const flushNow = useCallback(async () => {
    const id = raceIdRef.current;
    if (!id) return;
    if (flushingRef.current) return;
    if (
      gpsQueueRef.current.length === 0 &&
      imuQueueRef.current.length === 0 &&
      !pendingCalibrationRef.current
    ) {
      return;
    }

    flushingRef.current = true;
    // Snapshot what we're sending. New points captured during the round
    // trip stay in the queues and ride the next flush.
    const gpsBatch = gpsQueueRef.current.slice();
    const imuBatch = imuQueueRef.current.slice();
    const calBatch = pendingCalibrationRef.current;

    // Backend caps gps at 100, imu at 1000 per batch. We cap on the
    // client side too so a long offline buffer drains in chunks instead
    // of getting 413'd.
    const gpsChunk = gpsBatch.slice(0, FLUSH_GPS_BATCH_SIZE);
    const imuChunk = imuBatch.slice(0, 1000);

    const body = {
      gps: gpsChunk.map(gpsPointToWire),
      imu: imuChunk,
    };
    if (calBatch) {
      body.calibration = calBatch;
    }

    try {
      await apiFetch(`/api/races/${id}/telemetry`, {
        method: "POST",
        body,
      });

      // Drop acked GPS rows by recorded_at.
      const ackedGps = new Set(gpsChunk.map((p) => p.recorded_at));
      gpsQueueRef.current = gpsQueueRef.current.filter(
        (p) => !ackedGps.has(p.recorded_at),
      );
      writeJSON(gpsKey(id), gpsQueueRef.current);
      setQueueLength(gpsQueueRef.current.length);

      // Drop acked IMU rows by their `t` stamp.
      const ackedImu = new Set(imuChunk.map((s) => s.t));
      imuQueueRef.current = imuQueueRef.current.filter(
        (s) => !ackedImu.has(s.t),
      );
      writeJSON(imuKey(id), imuQueueRef.current);

      if (calBatch) {
        pendingCalibrationRef.current = null;
        clearKey(calKey(id));
        setPendingCalibration(null);
      }
      setError(null);
    } catch (e) {
      // Network failure, 401 (token expired), 5xx — keep the queues.
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

      gpsQueueRef.current.push(point);
      writeJSON(gpsKey(id), gpsQueueRef.current);
      setQueueLength(gpsQueueRef.current.length);

      setPoints((prev) => [...prev, point]);
      setLastPoint(point);

      if (gpsQueueRef.current.length >= FLUSH_GPS_BATCH_SIZE) {
        flushNow();
      }
    },
    [flushNow],
  );

  const onPositionError = useCallback((err) => {
    setError(err?.message || `geolocation error ${err?.code ?? ""}`);
  }, []);

  // ── IMU sampler ───────────────────────────────────────────────────
  const startImuSampler = useCallback(() => {
    if (imuIntervalRef.current) return;
    orientationHandleRef.current = startOrientationListener();
    const intervalMs = Math.round(1000 / IMU_SAMPLE_HZ);
    imuIntervalRef.current = setInterval(() => {
      const id = raceIdRef.current;
      if (!id) return;
      const raw = latestOrientation();
      const remapped = remapEulerToBoat(raw, phoneAxisRef.current);
      // remapped may be null until the first useful event arrives; we
      // also require yaw_deg because the backend column is NOT NULL.
      if (!remapped || remapped.yaw_deg == null) return;
      const sample = {
        t: new Date().toISOString(),
        heel_deg: remapped.heel_deg,
        pitch_deg: remapped.pitch_deg,
        yaw_deg: remapped.yaw_deg,
      };
      imuQueueRef.current.push(sample);
      writeJSON(imuKey(id), imuQueueRef.current);
    }, intervalMs);
  }, []);

  const stopImuSampler = useCallback(() => {
    if (imuIntervalRef.current) {
      clearInterval(imuIntervalRef.current);
      imuIntervalRef.current = null;
    }
    if (orientationHandleRef.current) {
      try {
        orientationHandleRef.current.stop();
      } catch {
        /* best effort */
      }
      orientationHandleRef.current = null;
    }
  }, []);

  // ── Calibration ───────────────────────────────────────────────────
  const captureCalibration = useCallback(() => {
    const raw = latestOrientation();
    const remapped = remapEulerToBoat(raw, phoneAxisRef.current);
    if (!remapped) {
      setError(
        "No orientation reading yet — wait a moment and try again.",
      );
      return null;
    }
    const id = raceIdRef.current;
    const cal = {
      captured_at: new Date().toISOString(),
      heel_zero_offset_deg: remapped.heel_deg,
      pitch_zero_offset_deg: remapped.pitch_deg,
    };
    pendingCalibrationRef.current = cal;
    setPendingCalibration(cal);
    if (id) writeJSON(calKey(id), cal);
    setError(null);
    return cal;
  }, []);

  // ── Start / stop ──────────────────────────────────────────────────
  const start = useCallback(async () => {
    if (!raceIdRef.current) {
      setError("No active race — set one before recording.");
      return;
    }
    // Idempotent — guard against double-start while a watcher is being
    // set up or is already live.
    if (watcherHandleRef.current || watcherPromiseRef.current) return;

    setError(null);
    setRecording(true);

    // Wake Lock — fire-and-forget; quietly no-ops on unsupported
    // browsers. We acquire BEFORE starting the watcher so the very
    // first GPS fix is captured with the screen guaranteed on.
    acquireWakeLock();

    // iOS DeviceOrientation permission. The `start` button click is
    // the user gesture; we must request inside this handler or the
    // promise rejects. Android returns "not-needed" instantly.
    let permResult = "not-needed";
    if (orientationNeedsPrompt()) {
      try {
        permResult = await requestOrientationPermission();
      } catch {
        permResult = "denied";
      }
    }
    setOrientationPermission(permResult);
    if (permResult === "granted" || permResult === "not-needed") {
      startImuSampler();
    }
    // If denied or unsupported, we silently skip IMU and record GPS-only.
    // The user can see the disposition via `orientationPermission`.

    // Kick off the GPS adapter. We store the promise so stop() can wait
    // for setup to finish if the user races start→stop too quickly.
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
  }, [
    onPosition,
    onPositionError,
    flushNow,
    acquireWakeLock,
    startImuSampler,
  ]);

  const stop = useCallback(async () => {
    // Wait for any in-flight watcher setup, then tear it down.
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
    stopImuSampler();
    if (flushTimerRef.current) {
      clearInterval(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    setRecording(false);
    // Final flush so the user doesn't have to wait 30s for the last
    // few seconds of points to ship.
    await flushNow();
    await releaseWakeLock();
    // If both queues drained cleanly, drop the localStorage entries so a
    // fresh recording session starts empty.
    const id = raceIdRef.current;
    if (
      id &&
      gpsQueueRef.current.length === 0 &&
      imuQueueRef.current.length === 0 &&
      !pendingCalibrationRef.current
    ) {
      clearKey(gpsKey(id));
      clearKey(imuKey(id));
      clearKey(calKey(id));
    }
  }, [flushNow, stopImuSampler, releaseWakeLock]);

  // ── Drain + reacquire on tab regain ───────────────────────────────
  useEffect(() => {
    const onOnline = () => {
      if (recording) flushNow();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        if (recording) {
          flushNow();
          // Wake Lock is released when the document hides; reacquire on
          // the way back so the next sleep timer doesn't catch us out.
          acquireWakeLock();
        }
      }
    };
    window.addEventListener("online", onOnline);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("online", onOnline);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [recording, flushNow, acquireWakeLock]);

  // ── Cleanup on unmount ───────────────────────────────────────────
  useEffect(() => {
    return () => {
      if (watcherHandleRef.current) {
        watcherHandleRef.current.stop().catch(() => {});
        watcherHandleRef.current = null;
      }
      if (imuIntervalRef.current) {
        clearInterval(imuIntervalRef.current);
        imuIntervalRef.current = null;
      }
      if (orientationHandleRef.current) {
        try {
          orientationHandleRef.current.stop();
        } catch {
          /* best effort */
        }
        orientationHandleRef.current = null;
      }
      if (flushTimerRef.current) {
        clearInterval(flushTimerRef.current);
        flushTimerRef.current = null;
      }
      if (wakeLockRef.current) {
        wakeLockRef.current.release().catch(() => {});
        wakeLockRef.current = null;
      }
    };
  }, []);

  return {
    recording,
    error,
    points,
    queueLength,
    lastPoint,
    pendingCalibration,
    orientationPermission,
    start,
    stop,
    flushNow,
    captureCalibration,
  };
}
