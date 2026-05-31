// packages/shared/src/markRounding.js
//
// JS mirror of `backend/app/services/mark_rounding.py`. Edit BOTH
// together — the algorithm and the threshold constants must stay
// aligned or the live UX (this file) will disagree with the
// authoritative server passes (Python).
//
// Used by `useAutoStopRecorder` and the mobile in-race view to show
// "rounded N of M marks · auto-stop in 4:32" without waiting for the
// next batch flush. The server remains the source of truth via the
// POST response's `mark_passes`, but the in-memory mirror gives the
// user immediate feedback.
//
// Algorithm (v3 — streaming sequential CPA, 2026-05-30):
//   * For the next-expected mark, track distance per sample.
//   * Track the running minimum-distance point (CPA) seen during the
//     current traversal.
//   * Detect departure: count consecutive samples where distance
//     strictly increased from the previous sample.
//   * When the departing count reaches DEPART_CONFIRM_SAMPLES AND the
//     running minimum is at or below the per-mark threshold, emit a
//     pass at the CPA timestamp/position and advance.
//   * Marks are detected strictly in order.
//   * If a single sample closes one rounding AND is also a closer
//     approach to the new next-expected mark, the chained emit is
//     queued and drained on the next feed() call (or by feed_batch's
//     drain loop).
//
// Why this changed from v2 — see the Python docstring. tl;dr: fixed-
// radius "enter then exit" missed every mark on distance races past
// nav structures (Colors Bravo 2026-05-30 recorded zero passes).

export const DEFAULT_DISTANCE_THRESHOLD_M = 250.0;
export const DEFAULT_INSHORE_THRESHOLD_M = 100.0;
export const FINAL_MARK_BONUS_M = 50.0;
export const DEPART_CONFIRM_SAMPLES = 3;

// Legacy aliases — old name was "radius". Deprecated; new callers
// should use threshold_m / DEFAULT_*_THRESHOLD_M. Kept so any third-
// party consumer of the module doesn't break at the import.
export const DEFAULT_RADIUS_M = DEFAULT_INSHORE_THRESHOLD_M;
export const FINAL_MARK_RADIUS_M =
  DEFAULT_INSHORE_THRESHOLD_M + FINAL_MARK_BONUS_M;

const EARTH_R_M = 6_371_000.0;

/**
 * Great-circle distance in metres.
 */
export function haversineM(lat1, lon1, lat2, lon2) {
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dp = ((lat2 - lat1) * Math.PI) / 180;
  const dl = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dp / 2) ** 2 +
    Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_R_M * Math.asin(Math.sqrt(a));
}

function normaliseThresholds(threshold, markCount) {
  if (typeof threshold === "number") {
    if (!(threshold > 0)) throw new Error("threshold_m must be positive");
    return new Array(markCount).fill(threshold);
  }
  if (!Array.isArray(threshold)) {
    throw new Error("threshold_m must be a number or array of numbers");
  }
  if (threshold.length !== markCount) {
    throw new Error(
      `threshold_m sequence length ${threshold.length} != mark count ${markCount}`,
    );
  }
  for (const t of threshold) {
    if (!(t > 0)) throw new Error("each threshold must be positive");
  }
  return threshold.slice();
}

/**
 * Per-mark threshold list for a course of `markCount` marks in `mode`
 * ("distance" or "inshore"; anything else treated as "distance"). The
 * final mark gets FINAL_MARK_BONUS_M extra so wide finish lines don't
 * get missed. Mirror of Python's `thresholds_for_course`.
 *
 * @param {number} markCount
 * @param {"distance" | "inshore"} [mode="distance"]
 * @returns {number[]}
 */
export function thresholdsForCourse(markCount, mode = "distance") {
  if (markCount <= 0) return [];
  const base =
    mode === "inshore"
      ? DEFAULT_INSHORE_THRESHOLD_M
      : DEFAULT_DISTANCE_THRESHOLD_M;
  const final = base + FINAL_MARK_BONUS_M;
  if (markCount === 1) return [final];
  const out = new Array(markCount - 1).fill(base);
  out.push(final);
  return out;
}

/**
 * Deprecated alias — mirror of Python's `radii_for_course`. Defaults
 * to "distance" mode for safety (wider tolerance — missing a mark is
 * worse than tripping early on a tight inshore course, which is rare
 * given sequential ordering).
 *
 * @param {number} markCount
 * @returns {number[]}
 */
export function radiiForCourse(markCount) {
  return thresholdsForCourse(markCount, "distance");
}

/**
 * Stateful detector. Mirror of Python's MarkRoundingDetector.
 *
 * @param {{lat: number, lon: number}[]} marks  course in order
 * @param {object} [opts]
 * @param {number | number[]} [opts.thresholdM]  scalar or per-mark list
 * @param {number | number[]} [opts.radiusM]  back-compat alias for thresholdM
 * @param {number} [opts.nextMarkIndex=0]  resume position
 * @param {number} [opts.departConfirmSamples=3]
 */
export class MarkRoundingDetector {
  constructor(
    marks,
    {
      thresholdM,
      radiusM,
      nextMarkIndex = 0,
      departConfirmSamples = DEPART_CONFIRM_SAMPLES,
    } = {},
  ) {
    if (nextMarkIndex < 0) throw new Error("nextMarkIndex must be >= 0");
    if (departConfirmSamples < 1)
      throw new Error("departConfirmSamples must be >= 1");
    const chosen =
      thresholdM !== undefined
        ? thresholdM
        : radiusM !== undefined
          ? radiusM
          : DEFAULT_DISTANCE_THRESHOLD_M;
    this._marks = marks.slice();
    this._thresholds = normaliseThresholds(chosen, this._marks.length);
    this._next = nextMarkIndex;
    this._departN = departConfirmSamples;
    this._queuedEmit = null;
    this._resetTraversal();
  }

  get nextMarkIndex() {
    return this._next;
  }

  get done() {
    return this._next >= this._marks.length;
  }

  _resetTraversal() {
    this._lastDist = null;
    this._minDist = null;
    this._minTs = null;
    this._minLat = null;
    this._minLon = null;
    this._departing = 0;
  }

  /**
   * Consume one point. Returns {markIndex, ts, lat, lon} if the point
   * completed a rounding, else null. The emitted ts/lat/lon are the
   * CPA sample captured during the traversal.
   *
   * @param {{lat: number, lon: number, ts: string|Date|number}} point
   */
  feed(point) {
    if (this.done) return null;

    const target = this._marks[this._next];
    const threshold = this._thresholds[this._next];
    const d = haversineM(point.lat, point.lon, target.lat, target.lon);

    if (this._minDist === null || d < this._minDist) {
      this._minDist = d;
      this._minTs = point.ts;
      this._minLat = point.lat;
      this._minLon = point.lon;
      // New minimum resets the departing run — we just got closer.
      this._departing = 0;
    } else if (this._lastDist !== null && d > this._lastDist) {
      // Strictly-increasing sample → count it as departure.
      this._departing += 1;
    }
    // Equal-or-decreased-but-not-new-min: hold the count.

    this._lastDist = d;

    if (
      this._departing >= this._departN &&
      this._minDist !== null &&
      this._minDist <= threshold
    ) {
      const emitted = {
        markIndex: this._next,
        ts: this._minTs,
        lat: this._minLat,
        lon: this._minLon,
      };
      this._next += 1;
      this._resetTraversal();
      const chained = this.feed(point);
      if (chained !== null) this._queuedEmit = chained;
      return emitted;
    }

    if (this._queuedEmit !== null) {
      const q = this._queuedEmit;
      this._queuedEmit = null;
      return q;
    }

    return null;
  }

  feedBatch(points) {
    const out = [];
    for (const p of points) {
      let r = this.feed(p);
      while (r !== null) {
        out.push(r);
        if (this._queuedEmit === null) {
          r = null;
        } else {
          r = this._queuedEmit;
          this._queuedEmit = null;
        }
      }
    }
    return out;
  }
}

/**
 * Convenience: full-track detection from scratch. Equivalent to the
 * Python `compute_passes`.
 *
 * @param {{lat: number, lon: number}[]} marks
 * @param {{lat: number, lon: number, ts: string|Date|number}[]} points
 * @param {number | number[]} [thresholdM]
 */
export function computePasses(marks, points, thresholdM) {
  const det = new MarkRoundingDetector(marks, { thresholdM });
  return det.feedBatch(points);
}
