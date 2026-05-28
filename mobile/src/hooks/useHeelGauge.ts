// useHeelGauge.ts — RN port of frontend/src/hooks/useHeelGauge.js.
//
// Reads expo-sensors DeviceMotion via `sensors/orientation`, remaps the
// W3C-shaped Euler to boat-frame heel/pitch/yaw using the shared
// `remapEulerToBoat` + `applyCalibration` helpers, and exposes the
// latest reading at a UI-friendly 5 Hz.
//
// Mirrors the web hook's contract so the orientation controls UI can be
// ported with minimal diffs. Differences:
//   * `supported` is sourced from `isSupported()` which checks for the
//     expo-sensors module (degrades to false if `npx expo install` hasn't
//     run yet for the dependency).
//   * No permission gating — Expo handles IMU permission natively on
//     iOS at first listener attach; on Android it's permission-free.

import { useEffect, useRef, useState } from "react";

import { applyCalibration, remapEulerToBoat } from "@sailline/shared";

import {
  isSupported,
  latest as latestOrientation,
  start as startListener,
} from "../sensors/orientation";

const TICK_HZ = 5;

export type PhoneAxis = "fore-aft" | "port-stbd";

export type Calibration = {
  heel_zero_offset_deg: number;
  pitch_zero_offset_deg: number;
  captured_at?: string;
};

export type HeelReading = {
  heelDeg: number;
  pitchDeg: number;
  yawDeg: number | null;
};

type Options = {
  enabled: boolean;
  phoneAxis?: PhoneAxis;
  polarityFlip?: boolean;
  calibration?: Calibration | null;
};

export function useHeelGauge({
  enabled,
  phoneAxis = "fore-aft",
  polarityFlip = false,
  calibration = null,
}: Options): { reading: HeelReading | null; supported: boolean } {
  const [reading, setReading] = useState<HeelReading | null>(null);
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
      if (!remapped) return; // sensor warming up — hold last value
      const corrected = applyCalibration(remapped, calRef.current);
      setReading({
        heelDeg: corrected.heel_deg,
        pitchDeg: corrected.pitch_deg,
        yawDeg: corrected.yaw_deg,
      });
    };
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

/**
 * Capture the current raw orientation reading as a zero-offset
 * calibration. Returns null if no fix is available yet (caller should
 * retry or surface a "waiting for sensor" hint).
 *
 * Note: this reads `latestOrientation()` once — it does NOT start the
 * listener. Call this from a screen that has the gauge mounted (which
 * is what triggers the listener subscription) so the orientation slot
 * is being kept fresh.
 */
export function captureCalibration(
  phoneAxis: PhoneAxis = "fore-aft",
  polarityFlip = false,
): Calibration | null {
  const raw = latestOrientation();
  const remapped = remapEulerToBoat(raw, phoneAxis, polarityFlip);
  if (!remapped) return null;
  return {
    heel_zero_offset_deg: remapped.heel_deg,
    pitch_zero_offset_deg: remapped.pitch_deg,
    captured_at: new Date().toISOString(),
  };
}
