// uploadStatus.ts — derive a coarse upload-health status from the
// recorder's LiveStats + NetInfo + wall clock.
//
// Phase 3 of the durable upload pipeline rework
// (sailline-docs/2026-06-01_durable-upload-pipeline-plan.md).
//
// The status drives the badge on the recording screen. It is the FIRST
// honest connectivity indicator the recorder has ever had — the
// existing "ON LINE" label on the GuidanceCard is cross-track distance
// to the next mark (navigation), not upload health.
//
// Pure: takes a snapshot of stats + a boolean for network reachability
// + the current wall clock; returns one of four enum values. No side
// effects, fully unit-testable on Windows where vitest runs.

import type { LiveStats } from "./debrief";

export type UploadStatus = "live" | "buffering" | "stalled" | "offline";

/** A flush success within this window keeps the status at "live".
 *  Tuned around our 30 s flush cadence — one missed cycle is healthy,
 *  two consecutive misses gets attention. */
export const LIVE_SUCCESS_FRESHNESS_MS = 60_000;

/** Queue depth at which "live" downgrades to "buffering" even when a
 *  recent success exists. With a 100-point batch cap and 1 Hz capture,
 *  10 in-queue = ~10 s of unsent fixes, well within healthy range, so
 *  this is a conservative threshold. */
export const BUFFERING_QUEUE_THRESHOLD = 10;

/** No successful upload for this long → "stalled". Five minutes is
 *  the smallest stretch that's almost certainly a real outage and
 *  not a coincidence of two slow flushes. */
export const STALLED_AGE_MS = 5 * 60_000;

/** Snapshot the derivation needs. Kept narrow so the caller doesn't
 *  have to pass the entire LiveStats blob (and so tests don't depend
 *  on fields the derivation doesn't read). */
export type StatusInputs = {
  /** ms-since-epoch of the most recent successful upload (200), or
   *  null if no successful upload has happened this session. */
  lastSuccessTs: number | null;
  /** Current depth of the unsent-points queue. Drives Buffering and
   *  Stalled labels. */
  queueDepth: number;
  /** Whether the OS reports the device has a usable network. NetInfo's
   *  isInternetReachable. ``null`` means "unknown yet" — treat as
   *  online (don't flag offline without evidence). */
  netReachable: boolean | null;
};

/**
 * Decide the recorder's current upload status.
 *
 * Priority order (top wins):
 *   1. offline — NetInfo says no network. Nothing's going anywhere.
 *   2. stalled — last success is older than STALLED_AGE_MS AND the
 *      queue has unsent points. (A drained queue + stale last-success
 *      is just "we're not recording," not a problem.)
 *   3. buffering — queue is large enough that we're not keeping up.
 *   4. live — last success within freshness window, queue small.
 *
 * The empty/at-rest case (no successes yet, no queue) reports "live"
 * — the recorder hasn't FAILED, it just hasn't done anything yet.
 * The badge can render this differently if we want; the recording
 * screen's mere presence already implies "we just started."
 */
export function deriveUploadStatus(
  inputs: StatusInputs,
  nowMs: number,
): UploadStatus {
  // 1. Offline trumps everything — we know we can't upload.
  if (inputs.netReachable === false) {
    return "offline";
  }

  // 2. Stalled — long silence with points still waiting.
  const ageSinceSuccess =
    inputs.lastSuccessTs != null
      ? nowMs - inputs.lastSuccessTs
      : Number.POSITIVE_INFINITY;
  if (
    inputs.queueDepth > 0 &&
    inputs.lastSuccessTs != null &&
    ageSinceSuccess > STALLED_AGE_MS
  ) {
    return "stalled";
  }

  // 3. Buffering — we've started building up unsent points.
  if (inputs.queueDepth >= BUFFERING_QUEUE_THRESHOLD) {
    return "buffering";
  }

  // 4. Default: live. No success yet AND no backlog is also fine —
  // recorder just started.
  if (inputs.lastSuccessTs == null) {
    return "live";
  }
  if (ageSinceSuccess <= LIVE_SUCCESS_FRESHNESS_MS) {
    return "live";
  }
  // Old success, no/small queue — call it stalled if we have ANY
  // backlog (covered above) else live. Falling through here means
  // queue is below threshold AND nothing fresh — treat as live so
  // the badge doesn't flicker red on a quiet pause.
  return "live";
}

/** Pure helper to translate the LiveStats blob into the StatusInputs
 *  shape. Lets the hook keep stats as the authoritative source and
 *  pass through to the pure function. */
export function statsToStatusInputs(
  stats: LiveStats,
  queueDepth: number,
  netReachable: boolean | null,
): StatusInputs {
  return {
    lastSuccessTs: stats.lastSuccessTs,
    queueDepth,
    netReachable,
  };
}
