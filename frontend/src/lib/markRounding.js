// frontend/src/lib/markRounding.js
//
// JS mirror of `backend/app/services/mark_rounding.py`. Edit BOTH
// together — the algorithm and the radius constant must stay aligned
// or the live UX (this file) will disagree with the authoritative
// server passes (Python).
//
// Used by `useAutoStopRecorder` to show "rounded N of M marks · auto-
// stop in 4:32" without waiting for the next batch flush. The server
// remains the source of truth via the POST response's `mark_passes`,
// but the in-memory mirror gives the user immediate feedback.
//
// Algorithm (must match Python):
//   * For mark i, rounding = entered the radius AND then exited.
//   * Marks are detected strictly in order — mark i+1 only after i.
//   * Default radius 50 m.
//   * State machine: outside→inside (no emit), inside→outside (emit).
//   * If a single point closes one rounding AND lands inside the next
//     mark's radius, record the entry on the same point.

export const DEFAULT_RADIUS_M = 50.0;
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
 * Stateful detector. Mirror of Python's MarkRoundingDetector.
 *
 * @param {{lat: number, lon: number}[]} marks  course in order
 * @param {object} [opts]
 * @param {number} [opts.radiusM=50]
 * @param {number} [opts.nextMarkIndex=0]  resume position
 */
export class MarkRoundingDetector {
  constructor(marks, { radiusM = DEFAULT_RADIUS_M, nextMarkIndex = 0 } = {}) {
    if (radiusM <= 0) throw new Error("radiusM must be positive");
    if (nextMarkIndex < 0) throw new Error("nextMarkIndex must be >= 0");
    this._marks = marks.slice();
    this._radiusM = radiusM;
    this._next = nextMarkIndex;
    this._inside = false;
  }

  get nextMarkIndex() {
    return this._next;
  }

  get done() {
    return this._next >= this._marks.length;
  }

  /**
   * Consume one point. Returns {markIndex, ts, lat, lon} if THIS point
   * closed a rounding, else null.
   *
   * @param {{lat: number, lon: number, ts: string|Date}} point
   */
  feed(point) {
    if (this.done) return null;

    const target = this._marks[this._next];
    const d = haversineM(point.lat, point.lon, target.lat, target.lon);
    const currentlyInside = d <= this._radiusM;

    let emitted = null;
    if (this._inside && !currentlyInside) {
      emitted = {
        markIndex: this._next,
        ts: point.ts,
        lat: point.lat,
        lon: point.lon,
      };
      this._next += 1;
      this._inside = false;

      if (!this.done) {
        const next = this._marks[this._next];
        const dNext = haversineM(point.lat, point.lon, next.lat, next.lon);
        if (dNext <= this._radiusM) this._inside = true;
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
 * @param {number} [radiusM]
 */
export function computePasses(marks, points, radiusM = DEFAULT_RADIUS_M) {
  const det = new MarkRoundingDetector(marks, { radiusM });
  return det.feedBatch(points);
}
