// debrief.ts — pure functions for assembling a RecorderDebrief blob.
//
// Phase 2 of the durable upload pipeline rework.
//
// useTrackRecorder maintains a small `uploadStats` ref that
// accumulates as flushes succeed and fail; on stop() the ref is
// snapshotted, combined with the ring buffer tail and device info,
// and POSTed to /api/races/{id}/recorder-debrief.
//
// All shape constants here MUST stay aligned with
// backend/app/routers/recorder_debrief.py — the schema version is
// the contract. Bump SCHEMA_VERSION on both sides if fields are
// renamed; additive changes are forward-compatible.

import { Platform } from "react-native";

import { RecorderLogEntry, tail } from "./recorderLog";

/** Debrief schema version. Backend rejects anything != 1 today. */
export const SCHEMA_VERSION = 1;

/** Default tail size shipped to the server inside the debrief. */
export const DEFAULT_RECENT_LOG_TAIL = 50;

// ── Types — mirror backend Pydantic models ────────────────────────────

export type DeviceInfo = {
  platform: "ios" | "android";
  os_version?: string;
  app_version?: string;
  build_id?: string;
};

export type SessionTiming = {
  start_ts: string; // ISO 8601
  end_ts: string;   // ISO 8601
  duration_s: number;
};

export type CaptureStats = {
  points_captured: number;
  points_uploaded: number;
  points_remaining_in_queue: number;
  max_queue_depth: number;
};

export type UploadStats = {
  attempts: number;
  successes: number;
  http_5xx: number;
  http_4xx: number;
  network_errors: number;
  longest_success_gap_s: number;
};

export type RecorderDebrief = {
  schema_version: number;
  device: DeviceInfo;
  session: SessionTiming;
  capture: CaptureStats;
  uploads: UploadStats;
  recent_log: RecorderLogEntry[];
};

/**
 * Live tally maintained in useTrackRecorder during a session. Pure
 * counters; turned into a UploadStats blob at stop() time.
 *
 * lastSuccessTs is the most recent successful flush; combined with
 * the current time we compute the "current gap" the UI badge will
 * render in Phase 3. longestSuccessGapSeen is updated lazily on
 * every flush attempt so the final debrief number reflects the worst
 * stretch of the session.
 */
export type LiveStats = {
  // Capture counters — set by the recorder's onPosition callback.
  pointsCaptured: number;
  pointsUploaded: number;       // sum of server-reported gps_inserted
  maxQueueDepth: number;

  // Upload counters — set in flushNow's success/failure branches.
  attempts: number;
  successes: number;
  http5xx: number;
  http4xx: number;
  networkErrors: number;

  // Wall clocks driving longest_success_gap_s.
  startedAt: number | null;     // ms since epoch — set on the first onPosition
  lastSuccessTs: number | null; // ms since epoch
  longestSuccessGapSeen: number; // seconds
};

/** Construct an empty LiveStats. Pure for testability. */
export function emptyLiveStats(): LiveStats {
  return {
    pointsCaptured: 0,
    pointsUploaded: 0,
    maxQueueDepth: 0,
    attempts: 0,
    successes: 0,
    http5xx: 0,
    http4xx: 0,
    networkErrors: 0,
    startedAt: null,
    lastSuccessTs: null,
    longestSuccessGapSeen: 0,
  };
}

/**
 * Compute the longest_success_gap that the LiveStats has observed
 * so far, considering both the recorded `longestSuccessGapSeen` AND
 * the in-flight gap since the last success up to `now`.
 *
 * Exported pure for unit testing — the recorder calls this at stop()
 * time to factor in any silence between the last successful flush
 * and the moment stop() ran.
 */
export function effectiveLongestGapSeconds(
  stats: LiveStats,
  nowMs: number,
): number {
  const inflightGap =
    stats.lastSuccessTs != null
      ? Math.max(0, (nowMs - stats.lastSuccessTs) / 1000)
      : 0;
  return Math.max(stats.longestSuccessGapSeen, inflightGap);
}

/** Best-effort device info. Mobile may not know every field; the
 *  backend treats them all as nullable. */
export function gatherDeviceInfo(opts?: {
  app_version?: string;
  build_id?: string;
  os_version?: string;
}): DeviceInfo {
  const platform: "ios" | "android" =
    Platform.OS === "ios" ? "ios" : "android";
  // Platform.Version is typed string|number; the backend column is
  // text, so coerce to string for stability.
  const os_version =
    opts?.os_version ??
    (Platform.Version != null ? String(Platform.Version) : undefined);
  return {
    platform,
    os_version,
    app_version: opts?.app_version,
    build_id: opts?.build_id,
  };
}

/**
 * Build the debrief blob ready for POST. Pure function — takes the
 * LiveStats snapshot, the ring buffer, the queue depth at stop time,
 * and the wall-clock endpoints; returns the JSON-serializable shape
 * the backend's Pydantic model accepts.
 */
export function buildDebrief(params: {
  stats: LiveStats;
  log: RecorderLogEntry[];
  queueDepthAtStop: number;
  endTsMs: number;
  device: DeviceInfo;
  tailEntries?: number;
}): RecorderDebrief {
  const { stats, log, queueDepthAtStop, endTsMs, device } = params;
  const startMs = stats.startedAt ?? endTsMs;
  const durationS = Math.max(0, Math.round((endTsMs - startMs) / 1000));

  const remainingInQueue = Math.max(
    0,
    stats.pointsCaptured - stats.pointsUploaded,
  );

  return {
    schema_version: SCHEMA_VERSION,
    device,
    session: {
      start_ts: new Date(startMs).toISOString(),
      end_ts: new Date(endTsMs).toISOString(),
      duration_s: durationS,
    },
    capture: {
      points_captured: stats.pointsCaptured,
      points_uploaded: stats.pointsUploaded,
      // Trust live queue measurement when present (it's the real-time
      // truth at stop); fall back to captured-minus-uploaded math
      // if the caller couldn't read the queue. Either matches the
      // server-side invariant that captured = uploaded + remaining.
      points_remaining_in_queue:
        queueDepthAtStop >= 0 ? queueDepthAtStop : remainingInQueue,
      max_queue_depth: stats.maxQueueDepth,
    },
    uploads: {
      attempts: stats.attempts,
      successes: stats.successes,
      http_5xx: stats.http5xx,
      http_4xx: stats.http4xx,
      network_errors: stats.networkErrors,
      longest_success_gap_s: effectiveLongestGapSeconds(stats, endTsMs),
    },
    recent_log: tail(log, params.tailEntries ?? DEFAULT_RECENT_LOG_TAIL),
  };
}
