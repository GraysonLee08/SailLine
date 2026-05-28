// sensors/orientation.ts — DeviceMotion → W3C-shaped {alpha, beta, gamma}
//
// The web app reads `DeviceOrientationEvent` directly. React Native has
// no equivalent global, but expo-sensors's `DeviceMotion` surfaces the
// same Euler angles via the device's IMU. We adapt its output to the
// W3C shape so we can reuse the shared `remapEulerToBoat` /
// `applyCalibration` helpers from `@sailline/shared` without forking.
//
// Units:
//   * Expo gives `rotation: { alpha, beta, gamma }` in RADIANS (per the
//     expo-sensors docs and native ObjC/Kotlin source). The W3C
//     DeviceOrientationEvent uses DEGREES. We convert here so the
//     downstream consumer is unit-agnostic and matches the web shape.
//   * `alpha` = compass yaw (0 = north on most devices once magnetometer
//     has a fix; some devices report relative-to-launch until calibrated
//     — that's fine, we expose it raw and let calibration zero it).
//   * `beta` = pitch (front/back tilt).
//   * `gamma` = roll (side-to-side tilt).
//
// Sample rate: we tick the listener at ~10 Hz which is what the recorder
// would want when we wire IMU capture in (Phase 4). The hook on top
// (useHeelGauge) samples this for the UI at 5 Hz; the listener doesn't
// know whether it's being sampled for UI or recording, so we run at the
// higher of the two rates.

let DeviceMotionImpl: any = null;
try {
  // Optional require so the app doesn't crash on metro start before
  // `npx expo install expo-sensors` has been run on Windows. The
  // orientation feature degrades gracefully to "not supported" until
  // the dependency is installed and a new dev client is built.
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  DeviceMotionImpl = require("expo-sensors").DeviceMotion;
} catch {
  DeviceMotionImpl = null;
}

const SAMPLE_HZ = 10;
const RAD_TO_DEG = 180 / Math.PI;

export type OrientationReading = {
  /** Yaw (compass heading), degrees in [0, 360). */
  alpha: number | null;
  /** Pitch (front/back tilt), degrees in [-180, 180]. */
  beta: number | null;
  /** Roll (side-to-side tilt), degrees in [-90, 90]. */
  gamma: number | null;
};

let latestReading: OrientationReading | null = null;
let subscription: { remove: () => void } | null = null;
let subscriberCount = 0;
let supportChecked = false;
let supportedCached = false;

/** Returns true once we've confirmed expo-sensors is available + the
 *  device has the IMU. Falsy in the simulator / before install. */
export function isSupported(): boolean {
  if (supportChecked) return supportedCached;
  supportChecked = true;
  supportedCached = !!DeviceMotionImpl;
  return supportedCached;
}

/** Latest sampled orientation, or null if no fix yet. */
export function latest(): OrientationReading | null {
  return latestReading;
}

/**
 * Subscribe to the device IMU. Returns a stop handle. Multiple subscribers
 * share the underlying listener — last-out closes it.
 *
 * The shared listener writes the latest reading into a module-level slot
 * so the UI hook can sample on its own clock instead of being driven by
 * every event tick (which on some devices arrives at >50 Hz).
 */
export function start(): { stop: () => void } {
  subscriberCount += 1;

  if (!DeviceMotionImpl) {
    // No-op handle so call sites don't need to special-case unsupported
    // devices — they'll just never see a reading.
    return {
      stop: () => {
        subscriberCount = Math.max(0, subscriberCount - 1);
      },
    };
  }

  if (!subscription) {
    // setUpdateInterval is in ms.
    try {
      DeviceMotionImpl.setUpdateInterval(Math.round(1000 / SAMPLE_HZ));
    } catch {
      /* not all platforms support tuning the interval; that's fine */
    }
    subscription = DeviceMotionImpl.addListener((data: any) => {
      const r = data?.rotation;
      if (!r) return;
      // Expo returns radians on both platforms in the current SDK. Some
      // values can be `null` until the sensor warms up; pass through.
      latestReading = {
        alpha: typeof r.alpha === "number" ? wrap360(r.alpha * RAD_TO_DEG) : null,
        beta: typeof r.beta === "number" ? r.beta * RAD_TO_DEG : null,
        gamma: typeof r.gamma === "number" ? r.gamma * RAD_TO_DEG : null,
      };
    });
  }

  return {
    stop: () => {
      subscriberCount = Math.max(0, subscriberCount - 1);
      if (subscriberCount === 0 && subscription) {
        try {
          subscription.remove();
        } catch {
          /* best effort */
        }
        subscription = null;
        latestReading = null;
      }
    },
  };
}

function wrap360(deg: number): number {
  let d = deg % 360;
  if (d < 0) d += 360;
  return d;
}
