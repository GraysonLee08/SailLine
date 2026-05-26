// packages/shared/src/markRounding.js
//
// JS mirror of `backend/app/services/mark_rounding.py`. Edit BOTH
// together — the algorithm and the radius constants must stay aligned
// or the live UX (this file) will disagree with the authoritative
// server passes (Python).
//
// Used by `useAutoStopRecorder` to show "rounded N of M marks · auto-
// stop in 4:32" without waiting for the next batch flush. The server
// remains the source of truth via the POST response's `mark_passes`,
// but the in-memory mirror gives the user immediate feedback.
//
// Algorithm (v2 — 2026-05-26, must match Python):
//   * For mark i, rounding = entered the radius AND then exited.
//   * Marks are detected strictly in order — mark i+1 only after i.
//   * Emitted timestamp/position is the CLOSEST-APPROACH point inside
//     the radius, NOT the exit point. Closer to "when you crossed the
//     line through the mark" — matters most for the finish, where
//     ended_at is set from this timestamp.
//   * Per-mark radii: constructor accepts either a scalar `radiusM`
//     (back-compat — applies to all marks) or a `radiiM` array.
//     `radiiForCourse(n)` returns [DEFAULT × (n-1), FINAL] — matches
//     the router's policy of giving the final mark a wider zone.
//   * State machine: outside→inside (no emit, start min-distance
//     tracking), inside→inside (update min-distance if closer),
//     inside→outside (emit pass at the min-distance point's timestamp).
//   * If a single point closes one rounding AND lands inside the next
//     mark's radius, record the entry on the same point.

export const DEFAULT_RADIUS_M = 50.0;
export const FINAL_MARK_RADIUS_M = 75.0;
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

/**
 * Coerce a scalar or per-mark array of radii into a length-N list.
 * Validates positivity and length match. Throws Error on bad input.
 */
function normaliseRadii(radius, markCount) {
  if (typeof radius === "number") {
    if (!(radius > 0)) throw new Error("radiusM must be positive");
    return new Array(markCount).fill(radius);
  }
  if (!Array.isArray(radius)) {
    throw new Error("radiusM must be a number or array of numbers");
  }
  if (radius.length !== markCount) {
    throw new Error(
      `radiusM sequence length ${radius.length} != mark count ${markCount}`,
    );
  }
  for (const r of radius) {
    if (!(r > 0)) throw new Error("each radius must be positive");
  }
  return radius.slice();
}

/**
 * Standard per-mark radius list for the router's policy: every mark
 * uses DEFAULT_RADIUS_M except the final mark which uses
 * FINAL_MARK_RADIUS_M. Mirror of Python's `radii_for_course`.
 *
 * @param {number} markCount
 * @returns {number[]}
 */
export function radiiForCourse(markCount) {
  if (markCount <= 0) return [];
  if (markCount === 1) return [FINAL_MARK_RADIUS_M];
  const out = new Array(markCount - 1).fill(DEFAULT_RADIUS_M);
  out.push(FINAL_MARK_RADIUS_M);
  return out;
}

/**
 * Stateful detector. Mirror of Python's MarkRoundingDetector.
 *
 * @param {{lat: number, lon: number}[]} marks  course in order
 * @param {object} [opts]
 * @param {number | number[]} [opts.radiusM=50]  scalar or per-mark list
 * @param {number} [opts.nextMarkIndex=0]  resume position
 */
export class MarkRoundingDetector {
  constructor(marks, { radiusM = DEFAULT_RADIUS_M, nextMarkIndex = 0 } = {}) {
    if (nextMarkIndex < 0) throw new Error("nextMarkIndex must be >= 0");
    this._marks = marks.slice();
    this._radii = normaliseRadii(radiusM, this._marks.length);
    this._next = nextMarkIndex;
    this._resetTraversalState();
  }

  get nextMarkIndex() {
    return this._next;
  }

  get done() {
    return this._next >= this._marks.length;
  }

  _resetTraversalState() {
    this._inside = false;
    this._minDistM = null;
    this._minDistTs = null;
    this._minDistLat = null;
    this._minDistLon = null;
  }

  /**
   * Consume one point. Returns {markIndex, ts, lat, lon} if THIS point
   * closed a rounding (i.e. we just exited the radius after having been
   * inside), else null. The emitted ts/lat/lon are the CLOSEST-APPROACH
   * point during the traversal, not this exit point.
   *
   * @param {{lat: number, lon: number, ts: string|Date}} point
   */
  feed(point) {
    if (this.done) return null;

    const target = this._marks[this._next];
    const radius = this._radii[this._next];
    const d = haversineM(point.lat, point.lon, target.lat, target.lon);
    const currentlyInside = d <= radius;

    // Track minimum-distance sample while inside the radius.
    if (currentlyInside) {
      if (this._minDistM === null || d < this._minDistM) {
        this._minDistM = d;
        this._minDistTs = point.ts;
        this._minDistLat = point.lat;
        this._minDistLon = point.lon;
      }
    }

    let emitted = null;
    if (this._inside && !currentlyInside) {
      // Exit transition — emit at the closest-approach sample.
      emitted = {
        markIndex: this._next,
        ts: this._minDistTs,
        lat: this._minDistLat,
        lon: this._minDistLon,
      };
      this._next += 1;
      this._resetTraversalState();

      if (!this.done) {
        const next = this._marks[this._next];
        const nextRadius = this._radii[this._next];
        const dNext = haversineM(point.lat, point.lon, next.lat, next.lon);
        if (dNext <= nextRadius) {
          this._inside = true;
          this._minDistM = dNext;
          this._minDistTs = point.ts;
          this._minDistLat = point.lat;
          this._minDistLon = point.lon;
        }
      }
    } else {
      this._inside = currentlyInside;
    }

    return emitted;
  }

  feedBatch(points) {
    const out = [];
    for (const p of points) {
      const r = this.feed(p);
      if (r) out.push(r);
    }
    return out;
  }
}

/**
 * Convenience: full-track detection from scratch. Equivalent to the
 * Python `compute_passes`. Used by tests and by the auto-stop hook
 * to recompute against the live in-memory point buffer.
 *
 * @param {{lat: number, lon: number}[]} marks
 * @param {{lat: number, lon: number, ts: string|Date}[]} points
 * @param {number | number[]} [radiusM]
 */
export function computePasses(marks, points, radiusM = DEFAULT_RADIUS_M) {
  const det = new MarkRoundingDetector(marks, { radiusM });
  return det.feedBatch(points);
}
