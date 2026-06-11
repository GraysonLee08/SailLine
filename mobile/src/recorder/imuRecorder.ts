// imuRecorder.ts — IMU capture + upload for the recorder (v1a, 2026-06-11).
//
// Spec: sailline -docs/2026-06-11_ai-tactician-spec.md. Samples the
// device IMU at 2 Hz (decimated from the shared 10 Hz orientation
// listener), remaps to boat frame using the race's phone-axis setting,
// and POSTs `{imu: [...], calibration?}` batches to the existing
// /telemetry endpoint on a 30 s cadence.
//
// Design decisions:
//
// * SEPARATE from the GPS pipeline. In native-uploader mode
//   Transistorsoft owns the GPS queue and POSTs directly — JS can't
//   inject IMU rows into those batches. The backend explicitly accepts
//   IMU-only batches (telemetry.py: "IMU-only — between GPS fixes"),
//   so IMU ships on its own queue in BOTH modes and the proven GPS
//   path is untouched.
//
// * RAW upload. The backend contract stores raw boat-frame values and
//   applies heel/pitch zero-offsets at read time from
//   race_calibrations. So we remap (phone → boat axes) but do NOT
//   subtract the zero offsets here. The calibration row itself is
//   uploaded alongside the next flush after it changes.
//
// * Self-contained settings. Phone axis + calibration live in
//   AsyncStorage under useOrientationSettings' key; this module reads
//   that key directly at each flush, so a mid-race "Zero" on any
//   screen is picked up within one flush cycle with zero call-site
//   coupling.
//
// * In-memory queue only (bounded). GPS is the race record; IMU is
//   enrichment. A crash loses ≤ the unflushed window rather than
//   adding another AsyncStorage write path on the hot loop. Flagged
//   as accepted debt in the session summary.
//
// * Samples with a null yaw (magnetometer warming up) are dropped —
//   the wire schema requires yaw_deg in [0, 360).

import AsyncStorage from "@react-native-async-storage/async-storage";

import { remapEulerToBoat } from "@sailline/shared";

import { apiFetch } from "../api";
import {
  isSupported,
  latest as latestOrientation,
  start as startListener,
} from "../sensors/orientation";

const SAMPLE_HZ = 2;
const FLUSH_INTERVAL_MS = 30_000;
const MAX_IMU_BATCH = 1000; // backend cap per batch
// Bound the in-memory queue: ~33 min of offline buffer at 2 Hz.
// Beyond that we drop oldest — by then the GPS gap is the real story.
const MAX_QUEUE = 4000;

const ORIENTATION_KEY_PREFIX = "sailline:orientation:";

type WireImuSample = {
  t: string;
  heel_deg: number;
  pitch_deg: number;
  yaw_deg: number;
};

type WireCalibration = {
  captured_at: string;
  heel_zero_offset_deg: number;
  pitch_zero_offset_deg: number;
};

type StoredOrientation = {
  phoneAxis?: "fore-aft" | "port-stbd";
  polarityFlip?: boolean;
  calibration?: {
    heel_zero_offset_deg: number;
    pitch_zero_offset_deg: number;
    captured_at?: string;
  } | null;
};

export type ImuCaptureHandle = {
  /** Final flush + listener teardown. Safe to call twice. */
  stop: () => Promise<void>;
};

async function loadOrientation(raceId: string): Promise<StoredOrientation> {
  try {
    const raw = await AsyncStorage.getItem(
      `${ORIENTATION_KEY_PREFIX}${raceId}`,
    );
    if (!raw) return {};
    return JSON.parse(raw) as StoredOrientation;
  } catch {
    return {};
  }
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/**
 * Start IMU capture + upload for a race. Returns null when the device
 * has no usable IMU (simulator, expo-sensors not installed) — callers
 * treat that as "feature unavailable," not an error.
 */
export async function startImuCapture(
  raceId: string,
): Promise<ImuCaptureHandle | null> {
  if (!isSupported()) return null;

  const settings = await loadOrientation(raceId);
  let phoneAxis = settings.phoneAxis === "port-stbd" ? "port-stbd" : "fore-aft";
  let polarityFlip = settings.polarityFlip === true;
  // The captured_at of the last calibration we successfully uploaded.
  // Anything newer found in AsyncStorage rides the next flush.
  let uploadedCalAt: string | null = null;
  let pendingCal: WireCalibration | null = null;
  if (settings.calibration) {
    pendingCal = {
      captured_at:
        settings.calibration.captured_at ?? new Date().toISOString(),
      heel_zero_offset_deg: clamp(
        settings.calibration.heel_zero_offset_deg, -90, 90),
      pitch_zero_offset_deg: clamp(
        settings.calibration.pitch_zero_offset_deg, -90, 90),
    };
  }

  const listener = startListener();
  const queue: WireImuSample[] = [];
  let stopped = false;
  let flushing = false;

  const sampleTimer = setInterval(() => {
    const raw = latestOrientation();
    const remapped = remapEulerToBoat(raw, phoneAxis, polarityFlip);
    if (!remapped) return; // sensor warming up
    if (remapped.yaw_deg == null) return; // wire schema requires yaw
    queue.push({
      t: new Date().toISOString(),
      heel_deg: clamp(remapped.heel_deg, -90, 90),
      pitch_deg: clamp(remapped.pitch_deg, -90, 90),
      yaw_deg: ((remapped.yaw_deg % 360) + 360) % 360,
    });
    if (queue.length > MAX_QUEUE) {
      queue.splice(0, queue.length - MAX_QUEUE); // drop oldest
    }
  }, Math.round(1000 / SAMPLE_HZ));

  const flush = async (): Promise<void> => {
    if (flushing) return;
    flushing = true;
    try {
      // Re-read settings each flush: picks up a mid-race "Zero" or an
      // axis change from any screen within one cycle, no coupling.
      const fresh = await loadOrientation(raceId);
      phoneAxis = fresh.phoneAxis === "port-stbd" ? "port-stbd" : "fore-aft";
      polarityFlip = fresh.polarityFlip === true;
      if (fresh.calibration?.captured_at &&
          fresh.calibration.captured_at !== uploadedCalAt) {
        pendingCal = {
          captured_at: fresh.calibration.captured_at,
          heel_zero_offset_deg: clamp(
            fresh.calibration.heel_zero_offset_deg, -90, 90),
          pitch_zero_offset_deg: clamp(
            fresh.calibration.pitch_zero_offset_deg, -90, 90),
        };
      }

      if (queue.length === 0 && pendingCal === null) return;
      const chunk = queue.slice(0, MAX_IMU_BATCH);
      const body: {
        imu: WireImuSample[];
        calibration?: WireCalibration;
      } = { imu: chunk };
      if (pendingCal) body.calibration = pendingCal;

      await apiFetch(`/api/races/${raceId}/telemetry`, {
        method: "POST",
        body,
      });
      // Acked: drop the chunk; calibration landed too.
      queue.splice(0, chunk.length);
      if (pendingCal) {
        uploadedCalAt = pendingCal.captured_at;
        pendingCal = null;
      }
    } catch {
      // Keep everything queued; next cycle retries. The GPS recorder's
      // status badge is the user-facing connectivity signal — IMU
      // stays silent by design.
    } finally {
      flushing = false;
    }
  };

  const flushTimer = setInterval(() => void flush(), FLUSH_INTERVAL_MS);

  return {
    stop: async () => {
      if (stopped) return;
      stopped = true;
      clearInterval(sampleTimer);
      clearInterval(flushTimer);
      listener.stop();
      await flush(); // final drain, best-effort
    },
  };
}
