// recorderLog.ts — bounded on-device ring buffer of recorder events.
//
// Phase 2 of the durable upload pipeline rework
// (sailline-docs/2026-06-01_durable-upload-pipeline-plan.md).
//
// Per-race AsyncStorage entry under `sailline.recorderLog.<raceId>`.
// Mirrors the queue.ts pattern so a crash/kill mid-race never loses
// the diagnostic history.
//
// IMPORTANT — coverage limitation. The ring buffer is populated by JS
// (in the flush callbacks of useTrackRecorder). When the React Native
// JS bridge sleeps — phone locked, app backgrounded — flushes pause
// and so do log appends. The buffer therefore captures exactly the
// intervals when JS was awake; long quiet stretches mean "JS asleep",
// NOT "everything was fine." Phase 4 (the Transistorsoft native
// uploader) rewires the source of these events from JS callbacks to
// the native onHttp event, which fires regardless of JS state.
//
// Until then, the log is the most accurate available picture of what
// the recorder DID DO; it's also a useful proxy for when JS was
// awake (and by inference, when it wasn't).

import AsyncStorage from "@react-native-async-storage/async-storage";

// ── Types — kept in sync with the backend Pydantic model ──────────────
//
// backend/app/routers/recorder_debrief.py::RecorderLogEntry
// If you add/rename a field, update both sides in the same change.

export type RecorderLogEntryKind = "flush" | "lifecycle" | "error";
export type RecorderLogEntryStatus = "ok" | "error" | "info";

export type RecorderLogEntry = {
  /** ISO-8601 timestamp of when this entry was recorded. */
  ts: string;
  kind: RecorderLogEntryKind;
  status?: RecorderLogEntryStatus;
  /** HTTP status code from the flush response, when applicable. */
  http_status?: number;
  /** Round-trip wall time for the request, ms. */
  duration_ms?: number;
  /** Number of points in the flushed batch. */
  batch_size?: number;
  /** Server-reported actually-inserted count (post-Phase-1 idempotency). */
  inserted?: number;
  /** Queue depth AFTER the operation completed. */
  queue_depth_after?: number;
  /**
   * Short human-readable note. Truncated to MESSAGE_MAX_CHARS chars
   * before storage to match the backend cap and bound on-device size.
   */
  message?: string;
};

// ── Limits ────────────────────────────────────────────────────────────

/**
 * Ring-buffer capacity. 200 entries at ~200 bytes each ≈ 40 KB of
 * AsyncStorage per race — trivial. We over-provision against the
 * 50-entry recent_log size shipped in the debrief so the debug
 * screen has more context than the server does.
 */
export const RING_BUFFER_SIZE = 200;

/**
 * Per-entry message cap. Matches the backend's MAX_LOG_MESSAGE_CHARS
 * so a long error message can't be truncated server-side after we've
 * already shipped it.
 */
export const MESSAGE_MAX_CHARS = 200;

const STORAGE_PREFIX = "sailline.recorderLog.";

function storageKey(raceId: string): string {
  return `${STORAGE_PREFIX}${raceId}`;
}

// ── Helpers ───────────────────────────────────────────────────────────

/**
 * Coerce an arbitrary entry into the storage shape: truncates the
 * message field, drops undefined values so AsyncStorage doesn't store
 * literal "undefined" strings, and stamps `ts` if the caller didn't.
 *
 * Exported for unit testing.
 */
export function normalizeEntry(entry: Partial<RecorderLogEntry>): RecorderLogEntry {
  const ts = entry.ts ?? new Date().toISOString();
  const kind: RecorderLogEntryKind = entry.kind ?? "lifecycle";
  const out: RecorderLogEntry = { ts, kind };
  if (entry.status !== undefined) out.status = entry.status;
  if (entry.http_status !== undefined) out.http_status = entry.http_status;
  if (entry.duration_ms !== undefined) out.duration_ms = entry.duration_ms;
  if (entry.batch_size !== undefined) out.batch_size = entry.batch_size;
  if (entry.inserted !== undefined) out.inserted = entry.inserted;
  if (entry.queue_depth_after !== undefined) {
    out.queue_depth_after = entry.queue_depth_after;
  }
  if (entry.message !== undefined && entry.message !== null) {
    out.message =
      entry.message.length > MESSAGE_MAX_CHARS
        ? entry.message.slice(0, MESSAGE_MAX_CHARS)
        : entry.message;
  }
  return out;
}

// ── Public API ────────────────────────────────────────────────────────

/**
 * Load the persisted ring buffer for a race. Returns [] on any error
 * — the log is best-effort diagnostic, never a correctness dependency.
 */
export async function loadLog(raceId: string): Promise<RecorderLogEntry[]> {
  try {
    const raw = await AsyncStorage.getItem(storageKey(raceId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as RecorderLogEntry[]) : [];
  } catch {
    return [];
  }
}

/**
 * Append an entry to a race's log, evicting the oldest if we've hit
 * RING_BUFFER_SIZE. Returns the new in-memory log so the caller can
 * use it without re-loading from storage. AsyncStorage write happens
 * asynchronously; failures are swallowed (best-effort).
 *
 * The in-memory caller is the authoritative copy during an active
 * session. AsyncStorage is a crash-survival copy that becomes the
 * source of truth only after JS restarts.
 */
export async function appendEntry(
  raceId: string,
  current: RecorderLogEntry[],
  entry: Partial<RecorderLogEntry>,
): Promise<RecorderLogEntry[]> {
  const normalized = normalizeEntry(entry);
  const next = current.length >= RING_BUFFER_SIZE
    ? [...current.slice(current.length - RING_BUFFER_SIZE + 1), normalized]
    : [...current, normalized];
  try {
    await AsyncStorage.setItem(storageKey(raceId), JSON.stringify(next));
  } catch {
    // Storage full / disabled — best effort.
  }
  return next;
}

/** Drop the persisted log for a race. Called after a successful debrief
 *  POST so the next session starts clean. Best-effort. */
export async function clearLog(raceId: string): Promise<void> {
  try {
    await AsyncStorage.removeItem(storageKey(raceId));
  } catch {
    // ignore
  }
}

/**
 * Return the tail of the buffer suitable for inlining in the debrief
 * blob. Default 50 matches the backend's MAX_RECENT_LOG_ENTRIES
 * headroom — large enough to diagnose, small enough not to bloat the
 * POST.
 */
export function tail(
  log: RecorderLogEntry[],
  n: number = 50,
): RecorderLogEntry[] {
  if (log.length <= n) return [...log];
  return log.slice(log.length - n);
}
