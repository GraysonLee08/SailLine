// packages/shared/src/nextMarkGuidance.js
//
// Pure functions for the live "next mark" guidance card during racing.
// Given the current GPS point + the course marks + the passes detected
// so far, returns the bearing, distance, and cross-track error to the
// next-up mark.
//
// Shared between web and mobile so the guidance values shown on both
// platforms agree exactly. The mark-rounding detector
// (`markRounding.js`) already owns "which mark is next" — this module
// only adds the navigation maths layered on top.
//
// Bearing convention: degrees clockwise from true north, in the range
// [0, 360). Same convention the rest of the app uses (wind direction,
// boat heading).
//
// Cross-track error: signed metres from the great-circle line through
// the PREVIOUS mark (or race-start point if no previous mark exists)
// and the next mark. Positive = right of the line, negative = left.
// Useful for "are we laying the mark on this tack?" UX.
//
// Returns null when there is no next mark (race finished).

import { haversineM, MarkRoundingDetector } from "./markRounding.js";

const EARTH_R_M = 6_371_000.0;

/**
 * Initial bearing (forward azimuth) from (lat1,lon1) to (lat2,lon2),
 * in degrees clockwise from true north. Standard great-circle formula.
 */
export function initialBearingDeg(lat1, lon1, lat2, lon2) {
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dl = ((lon2 - lon1) * Math.PI) / 180;
  const y = Math.sin(dl) * Math.cos(p2);
  const x =
    Math.cos(p1) * Math.sin(p2) -
    Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  const theta = Math.atan2(y, x);
  return ((theta * 180) / Math.PI + 360) % 360;
}

/**
 * Signed cross-track distance (metres) from a point to the great-circle
 * line through (lat1,lon1)→(lat2,lon2). Positive = right of the line in
 * the direction of travel; negative = left.
 *
 * Standard formula: d_xt = asin(sin(d13/R) * sin(θ13 - θ12)) * R
 */
export function crossTrackErrorM(
  lat1, lon1,        // line start
  lat2, lon2,        // line end (also the destination)
  latP, lonP,        // observer
) {
  const d13 = haversineM(lat1, lon1, latP, lonP);
  if (d13 === 0) return 0;
  const theta13 = (initialBearingDeg(lat1, lon1, latP, lonP) * Math.PI) / 180;
  const theta12 = (initialBearingDeg(lat1, lon1, lat2, lon2) * Math.PI) / 180;
  return Math.asin(Math.sin(d13 / EARTH_R_M) * Math.sin(theta13 - theta12)) * EARTH_R_M;
}

/**
 * @typedef GuidanceInput
 * @property {{lat:number,lon:number,name?:string}[]} marks  Course in order.
 * @property {{lat:number,lon:number,ts:string|Date}[]} points  Track points so far.
 *           Used (with `marks`) to determine which mark is next by
 *           replaying the detector — same source of truth as the auto-
 *           stop hook and the server's mark_rounding detector.
 * @property {{lat:number,lon:number}} current  Latest GPS point.
 * @property {number | number[]} [radiusM]  Per-mark or scalar rounding radius.
 */

/**
 * @typedef GuidanceResult
 * @property {number} nextMarkIndex     0-based index of the next mark to round.
 * @property {{lat:number,lon:number,name?:string}} nextMark  The next mark.
 * @property {number} distanceM         Great-circle distance to the next mark.
 * @property {number} bearingDeg        True bearing to the next mark.
 * @property {number} crossTrackM       Signed metres from the active leg line.
 *                                       Positive = right of line, negative = left.
 * @property {{lat:number,lon:number} | null} fromMark  The mark we're sailing
 *           from, used as the start of the leg line. Null when sailing the
 *           first leg (the line origin is the current point itself, so
 *           crossTrackM is reported as 0).
 */

/**
 * Compute the live guidance for the current point. Returns null if the
 * race is already finished (all marks rounded) or input is invalid.
 *
 * @param {GuidanceInput} input
 * @returns {GuidanceResult | null}
 */
export function computeGuidance({ marks, points, current, radiusM = 50 }) {
  if (!Array.isArray(marks) || marks.length === 0) return null;
  if (!current || !Number.isFinite(current.lat) || !Number.isFinite(current.lon)) {
    return null;
  }

  // Replay the existing track to find the next-mark index. This matches
  // how the server determines "next mark"; using a fresh detector
  // (instead of trusting any client-side mark_passes cache) is correct
  // even if the client UI hasn't refreshed yet.
  const detector = new MarkRoundingDetector(marks, { radiusM });
  if (Array.isArray(points) && points.length > 0) {
    detector.feedBatch(points);
  }
  if (detector.done) return null;

  const nextIdx = detector.nextMarkIndex;
  const next = marks[nextIdx];
  const distanceM = haversineM(current.lat, current.lon, next.lat, next.lon);
  const bearingDeg = initialBearingDeg(
    current.lat, current.lon, next.lat, next.lon,
  );

  // Active leg line: previous mark → next mark. For the very first leg
  // there's no previous mark, so we can't compute cross-track from a
  // line — report 0 and null `fromMark`.
  let crossTrackM = 0;
  const fromMark = nextIdx > 0 ? marks[nextIdx - 1] : null;
  if (fromMark) {
    crossTrackM = crossTrackErrorM(
      fromMark.lat, fromMark.lon,
      next.lat, next.lon,
      current.lat, current.lon,
    );
  }

  return {
    nextMarkIndex: nextIdx,
    nextMark: next,
    distanceM,
    bearingDeg,
    crossTrackM,
    fromMark,
  };
}
