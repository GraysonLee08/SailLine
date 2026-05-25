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
 * @param {boolean} polarityFlip  true when the phone is rotated 180°
 *                                relative to its canonical pose (top
 *                                of phone pointing aft instead of
 *                                forward in "fore-aft", or pointing
 *                                port instead of starboard in
 *                                "port-stbd"). Negates heel and pitch.
 * @returns {{heel_deg:number, pitch_deg:number, yaw_deg:number} | null}
 */
export function remapEulerToBoat(
  orientation,
  axis = DEFAULT_PHONE_AXIS,
  polarityFlip = false,
) {
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

  if (polarityFlip) {
    // Phone is mounted 180° from its canonical pose — both the
    // "forward" and "right" device edges have swapped sign relative to
    // the boat frame.
    heel = -heel;
    pitch = -pitch;
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

// ─── Phone-axis auto-detect ────────────────────────────────────────────
//
// Goal: pick the right ``axis`` (and detect a 180° polarity flip) by
// comparing the phone's compass heading (device-alpha) against the
// boat's GPS course over ground while the boat is actually moving in a
// roughly straight line.
//
// Premise: when the phone is laid flat on the boat with the top edge
// pointing forward ("fore-aft, normal polarity"), device-alpha (which
// is the compass heading of the top of the phone) should match GPS COG
// to within sensor noise — typically ±15° once GPS is settled.
//
// If the phone is rotated 90° clockwise relative to that canonical
// pose, alpha leads COG by 90° → axis = "port-stbd". A 180° rotation
// keeps the axis but flips the sign of heel ("polarity flip"). 270° is
// port-stbd with polarity flip.
//
// We score all four candidate rotations (0°, 90°, 180°, 270°) and pick
// the smallest absolute delta. Confidence is the ratio between the
// best candidate's delta and the second-best — a sharp winner is high
// confidence, a near-tie is low. The caller decides whether to act on
// a low-confidence detection.

/**
 * Smallest angular delta between two compass-bearing-style angles, in
 * the range [0, 180]. Exported for tests; useful elsewhere.
 */
export function angleDelta(a, b) {
  if (!num(a) || !num(b)) return null;
  let d = ((a - b) % 360 + 360) % 360;
  if (d > 180) d = 360 - d;
  return d;
}

/**
 * Decide phone axis + polarity from a single GPS-COG and device-alpha
 * pair. Pure function; the caller is responsible for filtering to
 * samples where the boat is actually moving (sog > ~1.5 kt) and the
 * compass has settled.
 *
 * @param {object} opts
 * @param {number} opts.cog            GPS course over ground, degrees true
 * @param {number} opts.alpha          DeviceOrientationEvent.alpha
 * @returns {{axis: string, polarityFlip: boolean,
 *           delta: number, confidence: number} | null}
 *
 * Returns null if either input is missing/non-finite.
 *
 * Confidence is 1 - (best_delta / second_best_delta), clamped to [0, 1].
 * A confidence of 0 means two candidates are tied — caller should defer.
 */
export function detectPhoneAxis({ cog, alpha } = {}) {
  if (!num(cog) || !num(alpha)) return null;
  // Each candidate maps a "what would alpha be if the phone were in
  // this orientation, given this COG" expectation, and we score how
  // close the observed alpha actually is to that expectation.
  //
  // Canonical convention (matches remapEulerToBoat above):
  //   fore-aft, normal:  alpha = COG
  //   port-stbd, normal: alpha = COG + 90  (top of phone points stbd)
  //   fore-aft, flipped: alpha = COG + 180 (top of phone points aft)
  //   port-stbd, flipped: alpha = COG + 270 (top of phone points port)
  const candidates = [
    { axis: "fore-aft",  polarityFlip: false, expected: cog },
    { axis: "port-stbd", polarityFlip: false, expected: cog + 90 },
    { axis: "fore-aft",  polarityFlip: true,  expected: cog + 180 },
    { axis: "port-stbd", polarityFlip: true,  expected: cog + 270 },
  ];
  const scored = candidates
    .map((c) => ({ ...c, delta: angleDelta(alpha, c.expected) }))
    .sort((a, b) => a.delta - b.delta);
  const best = scored[0];
  const second = scored[1];
  // Confidence: how much better is the winner than the runner-up?
  // If best.delta is 5° and second.delta is 90°, confidence ≈ 0.94.
  // If they're 40° and 50°, confidence ≈ 0.20 — close to a coin flip.
  let confidence = 0;
  if (second.delta > 0) {
    confidence = 1 - best.delta / second.delta;
    if (confidence < 0) confidence = 0;
    if (confidence > 1) confidence = 1;
  } else {
    // Both at zero (impossible in practice with continuous angles).
    confidence = 0;
  }
  return {
    axis: best.axis,
    polarityFlip: best.polarityFlip,
    delta: best.delta,
    confidence,
  };
}

/**
 * Stateful detector that requires N consistent observations before
 * locking in a result. Filters caller-side noise — a single bad GPS
 * fix or compass glitch won't flip the axis on its own.
 *
 * Usage:
 *   const det = createAxisDetector({ minSogKts: 1.5, minSamples: 6,
 *                                    minConfidence: 0.4 });
 *   for each sample:
 *     const res = det.consider({ cog, alpha, sog_kts });
 *     if (res) { use res.axis / res.polarityFlip; stop calling consider }
 *
 * The detector locks the first time it accumulates ``minSamples`` in a
 * row that agree on the same (axis, polarityFlip) pair, each above
 * ``minConfidence``. Disagreement resets the streak. After locking,
 * ``consider`` returns the same result on every subsequent call.
 */
export function createAxisDetector(opts = {}) {
  const minSogKts = num(opts.minSogKts) ? opts.minSogKts : 1.5;
  const minSamples = Math.max(1, opts.minSamples ?? 6);
  const minConfidence = num(opts.minConfidence) ? opts.minConfidence : 0.4;

  let locked = null;
  let streak = 0;
  let streakKey = null;

  return {
    /**
     * Feed one sample. Returns the locked result once the detector
     * converges, or null while still gathering. After a result is
     * locked, subsequent calls return the same object.
     */
    consider({ cog, alpha, sog_kts } = {}) {
      if (locked) return locked;
      if (!num(sog_kts) || sog_kts < minSogKts) return null;
      const det = detectPhoneAxis({ cog, alpha });
      if (!det) return null;
      if (det.confidence < minConfidence) {
        streak = 0;
        streakKey = null;
        return null;
      }
      const key = `${det.axis}|${det.polarityFlip}`;
      if (key === streakKey) {
        streak += 1;
      } else {
        streakKey = key;
        streak = 1;
      }
      if (streak >= minSamples) {
        locked = {
          axis: det.axis,
          polarityFlip: det.polarityFlip,
          confidence: det.confidence,
        };
        return locked;
      }
      return null;
    },
    /** Currently-locked result, or null. Read-only accessor. */
    get result() { return locked; },
    /** Reset for tests / re-detection. */
    reset() {
      locked = null;
      streak = 0;
      streakKey = null;
    },
  };
}
