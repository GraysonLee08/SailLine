// useHeelGauge — read DeviceOrientationEvent at a UI-friendly rate and
// expose the latest heel/pitch for live display in the race overlay.
//
// Separate from `useTrackRecorder` on purpose: the recorder samples at
// 10 Hz and queues to the offline buffer for `/telemetry` flush; the
// gauge ticks at ~5 Hz only when we have a visible UI consumer. Cutting
// the UI rate avoids re-render churn (5 Hz is well below human flicker
// fusion for a numeric readout).
//
// Permission acquisition for iOS is *not* triggered by this hook — the
// recorder's `start()` is the canonical user-gesture entry point for the
// orientation permission. If the gauge mounts while permission hasn't
// been granted, `reading` stays null and the UI should show a dash.

import { useEffect, useRef, useState } from "react";

import { applyCalibration, remapEulerToBoat } from "../lib/imuAxes";
import { isSupported, latest as latestOrientation, start as startListener } from "../sensors/orientation";

const TICK_HZ = 5;

/**
 * @param {object}   opts
 * @param {boolean}  opts.enabled        Master gate. Off → no listener, no ticks.
 * @param {string}   [opts.phoneAxis]    "fore-aft" | "port-stbd". Default fore-aft.
 * @param {boolean}  [opts.polarityFlip] When true the phone is mounted 180°
 *                                       from canonical (top toward stern / port).
 *                                       Negates heel/pitch so the gauge tracks
 *                                       the boat frame consistently with the
 *                                       recorder's auto-detect result.
 * @param {object}   [opts.calibration]  Optional client-side zero offsets
 *                                       `{heel_zero_offset_deg, pitch_zero_offset_deg}`.
 * @returns {{ reading: {heelDeg:number, pitchDeg:number, yawDeg:number|null} | null,
 *            supported: boolean }}
 */
export function useHeelGauge({
  enabled,
  phoneAxis = "fore-aft",
  polarityFlip = false,
  calibration = null,
} = {}) {
  const [reading, setReading] = useState(null);
  const supportedRef = useRef(isSupported());

  // Keep the latest calibration / axis in refs so the tick interval
  // doesn't need to re-create when they change.
  const axisRef = useRef(phoneAxis);
  axisRef.current = phoneAxis;
  const flipRef = useRef(polarityFlip);
  flipRef.current = polarityFlip;
  const calRef = useRef(calibration);
  calRef.current = calibration;

  useEffect(() => {
    if (!enabled || !supportedRef.current) {
      setReading(null);
      return undefined;
    }
    const handle = startListener();
    const intervalMs = Math.round(1000 / TICK_HZ);
    const tick = () => {
      const raw = latestOrientation();
      const remapped = remapEulerToBoat(raw, axisRef.current, flipRef.current);
      if (!remapped) {
        // No usable reading yet (sensors warming up). Hold the last
        // value if we have one — flicker-to-null reads as a fault.
        return;
      }
      const corrected = applyCalibration(remapped, calRef.current);
      setReading({
        heelDeg: corrected.heel_deg,
        pitchDeg: corrected.pitch_deg,
        yawDeg: corrected.yaw_deg,
      });
    };
    // First tick on next animation frame so the gauge appears as soon
    // as one event has fired.
    const initialTimer = setTimeout(tick, 50);
    const id = setInterval(tick, intervalMs);
    return () => {
      clearTimeout(initialTimer);
      clearInterval(id);
      handle.stop();
      setReading(null);
    };
  }, [enabled]);

  return { reading, supported: supportedRef.current };
}
