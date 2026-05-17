// imuAxes.js — pure helpers for mapping the W3C DeviceOrientationEvent's
// device-frame Euler angles into the boat frame the backend expects.
//
// The phone reports orientation as three angles tied to the device body:
//
//   alpha — 0..360, compass heading (yaw). 0 = north, 90 = east.
//   beta  — -180..180, front-to-back tilt around the device x-axis.
//           Positive = front edge tipping up when device is flat.
//   gamma — -90..90, left-to-right tilt around the device y-axis.
//           Positive = right edge tipping down (so left edge up) when
//           device is flat.
//
// The backend imu_samples columns assume the boat frame:
//
//   heel_deg  — positive = starboard rail down
//   pitch_deg — positive = bow up
//   yaw_deg   — 0..360 degrees true, 0 = north
//
// Which W3C angle becomes "heel" vs "pitch" depends on how the phone is
// oriented relative to the boat's centerline. We expose two orientations
// covering the two common phone-on-table positions:
//
//   "fore-aft"  — phone long edge along the boat's centerline, screen up,
//                 top of the phone (camera) pointing forward.
//                  → boat-pitch  = device-beta  (front-to-back = bow-to-stern)
//                  → boat-heel   = device-gamma (left-to-right = port-stbd)
//                 BUT: signs depend on which way the top of the phone
//                 points. We treat "top pointing forward" as canonical;
//                 if the user lays the phone with the top toward the
//                 stern, the toggle is still "fore-aft" but signs flip.
//                 In practice this comes out in the wash via calibration —
//                 the zero-offset captures resting orientation. We do not
//                 try to detect orientation polarity automatically.
//   "port-stbd" — phone long edge across the boat, screen up, top of the
//                 phone pointing toward starboard.
//                  → boat-pitch  = device-gamma  (left-to-right = bow-to-stern)
//                  → boat-heel   = device-beta   (front-to-back = port-stbd)
//
// Yaw mapping: device-alpha is compass heading regardless of axis. We
// pass it through, normalising to [0, 360).
//
// All inputs are tolerated as null/undefined — the W3C event delivers
// nulls in the first frame or two before the sensors have settled. The
// remap returns null in those cases so the caller can skip the sample.

export const PHONE_AXES = Object.freeze(["fore-aft", "port-stbd"]);

export const DEFAULT_PHONE_AXIS = "fore-aft";

/**
 * True iff the value is a finite number we can safely use as an angle.
 */
function num(x) {
  return typeof x === "number" && Number.isFinite(x);
}

/**
 * Wrap a value into [0, 360).
 */
export function wrap360(deg) {
  if (!num(deg)) return null;
  let v = deg % 360;
  if (v < 0) v += 360;
  return v;
}

/**
 * Clamp a value into the closed range [lo, hi].
 */
function clamp(v, lo, hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

/**
 * Remap a W3C DeviceOrientationEvent reading into boat-frame
 * heel/pitch/yaw. Returns null when any required input is missing — the
 * caller should drop the sample rather than send a partial row.
 *
 * @param {object}  orientation   { alpha, beta, gamma } from the event
 * @param {string}  axis          one of PHONE_AXES
 * @returns {{heel_deg:number, pitch_deg:number, yaw_deg:number} | null}
 */
export function remapEulerToBoat(orientation, axis = DEFAULT_PHONE_AXIS) {
  if (!orientation) return null;
  const { alpha, beta, gamma } = orientation;
  if (!num(beta) || !num(gamma)) return null;

  let heel;
  let pitch;
  if (axis === "port-stbd") {
    // Long edge across the boat: device-beta is the across-boat tilt
    // (heel), device-gamma is the along-boat tilt (pitch).
    heel = beta;
    pitch = gamma;
  } else {
    // Default fore-aft layout.
    heel = gamma;
    pitch = beta;
  }

  // Backend Pydantic schema bounds heel/pitch to [-90, 90]. The W3C
  // beta range is technically [-180, 180]; if the phone is tipped past
  // vertical (e.g. screen down), beta wraps. We clamp rather than wrap
  // because anything past ±90 is the phone being mishandled, not the
  // boat doing 90 of heel.
  heel = clamp(heel, -90, 90);
  pitch = clamp(pitch, -90, 90);

  const yaw = wrap360(alpha);
  // Yaw is required by the backend schema but may briefly be null
  // before the magnetometer has a fix. Caller can choose to drop the
  // sample or fall back to GPS COG; we surface the null and let them
  // decide.
  return {
    heel_deg: heel,
    pitch_deg: pitch,
    yaw_deg: yaw,
  };
}

/**
 * Subtract a calibration zero-offset from a heel/pitch reading. Returns
 * the corrected pair. Pass null offsets to leave the field untouched.
 *
 * The backend ALSO applies offsets at read-time (storing raw imu_samples
 * + race_calibrations history). This client-side variant is for the
 * live gauge in the race overlay — we want the gauge to read ~0 right
 * after the user hits "Zero" without waiting for a server round trip.
 */
export function applyCalibration(reading, offsets) {
  if (!reading) return null;
  const heelOff = num(offsets?.heel_zero_offset_deg)
    ? offsets.heel_zero_offset_deg
    : 0;
  const pitchOff = num(offsets?.pitch_zero_offset_deg)
    ? offsets.pitch_zero_offset_deg
    : 0;
  return {
    heel_deg: clamp(reading.heel_deg - heelOff, -90, 90),
    pitch_deg: clamp(reading.pitch_deg - pitchOff, -90, 90),
    yaw_deg: reading.yaw_deg, // yaw is not calibrated
  };
}
