// frontend/src/lib/markRounding.test.js
//
// Mirror of `backend/tests/test_mark_rounding.py`. Same scenarios so
// the JS port stays algorithmically identical to the Python source of
// truth.
//
// Geometry helpers are duplicated here rather than imported from the
// hook test files — the `markRounding` lib is the lowest tier and
// shouldn't depend on test plumbing elsewhere in the tree.
//
// v3 (2026-05-30): streaming sequential CPA. The fixed-radius
// enter/exit semantics from v2 are gone — assertions updated to match.

import { describe, it, expect } from "vitest";

import {
  computePasses,
  DEFAULT_DISTANCE_THRESHOLD_M,
  DEFAULT_INSHORE_THRESHOLD_M,
  DEFAULT_RADIUS_M,
  DEPART_CONFIRM_SAMPLES,
  FINAL_MARK_BONUS_M,
  FINAL_MARK_RADIUS_M,
  haversineM,
  MarkRoundingDetector,
  radiiForCourse,
  thresholdsForCourse,
} from "@sailline/shared";

const REF_LAT = 42.05;
const REF_LON = -87.75;

const mToDLat = (m) => m / 111_000;
const mToDLon = (m, atLat = REF_LAT) =>
  m / (111_000 * Math.cos((atLat * Math.PI) / 180));

function offset(lat, lon, bearingDeg, distM) {
  const rad = (bearingDeg * Math.PI) / 180;
  const dlat = mToDLat(distM * Math.cos(rad));
  const dlon = mToDLon(distM * Math.sin(rad), lat);
  return { lat: lat + dlat, lon: lon + dlon };
}

function lineThrough(
  mark,
  closestM,
  { spanM = 600, n = 31, bearingDeg = 90, t0 = 0, dtS = 1 } = {},
) {
  const perp = (bearingDeg + 90) % 360;
  const cap = offset(mark.lat, mark.lon, perp, closestM);
  const half = spanM / 2;
  const step = n > 1 ? spanM / (n - 1) : 0;
  const out = [];
  for (let i = 0; i < n; i += 1) {
    const dAlong = -half + i * step;
    const p = offset(cap.lat, cap.lon, bearingDeg, dAlong);
    out.push({
      lat: p.lat,
      lon: p.lon,
      ts: new Date((t0 + i * dtS) * 1000).toISOString(),
    });
  }
  return out;
}

describe("markRounding (v3 streaming CPA)", () => {
  it("haversine round-trips a known distance", () => {
    const d = haversineM(0, 0, 1, 0);
    expect(d).toBeGreaterThan(110_000);
    expect(d).toBeLessThan(112_000);
  });

  it("threshold constants match the Python sibling", () => {
    expect(DEFAULT_DISTANCE_THRESHOLD_M).toBe(250);
    expect(DEFAULT_INSHORE_THRESHOLD_M).toBe(100);
    expect(FINAL_MARK_BONUS_M).toBe(50);
    expect(DEPART_CONFIRM_SAMPLES).toBe(3);
    // Legacy aliases keep mapping to the inshore numbers — see Python
    // test_threshold_constants_tripwire.
    expect(DEFAULT_RADIUS_M).toBe(DEFAULT_INSHORE_THRESHOLD_M);
    expect(FINAL_MARK_RADIUS_M).toBe(
      DEFAULT_INSHORE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    );
  });

  it("emits one pass for a tight pass in inshore mode", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const track = lineThrough(mark, 10);
    const passes = computePasses(
      [mark],
      track,
      DEFAULT_INSHORE_THRESHOLD_M,
    );
    expect(passes).toHaveLength(1);
    expect(passes[0].markIndex).toBe(0);
  });

  it("v3 win — 200 m wide pass detected in distance mode (v2 missed this)", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const track = lineThrough(mark, 200);
    const passes = computePasses(
      [mark],
      track,
      DEFAULT_DISTANCE_THRESHOLD_M,
    );
    expect(passes).toHaveLength(1);
  });

  it("200 m wide pass NOT detected in inshore mode", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const track = lineThrough(mark, 200);
    expect(
      computePasses([mark], track, DEFAULT_INSHORE_THRESHOLD_M),
    ).toEqual([]);
  });

  it("very wide (800 m) pass never detected", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const track = lineThrough(mark, 800);
    expect(
      computePasses([mark], track, DEFAULT_DISTANCE_THRESHOLD_M),
    ).toEqual([]);
  });

  it("ignores later marks crossed before earlier ones round", () => {
    const a = offset(REF_LAT, REF_LON, 0, 2000);
    const aMark = { lat: a.lat, lon: a.lon };
    const bMark = { lat: REF_LAT, lon: REF_LON };

    const leg1 = lineThrough(bMark, 20, { t0: 0 });
    const leg2 = lineThrough(aMark, 20, { t0: 200 });
    const leg3 = lineThrough(bMark, 20, { t0: 500 });

    const passes = computePasses([aMark, bMark], [...leg1, ...leg2, ...leg3]);
    expect(passes.map((p) => p.markIndex)).toEqual([0, 1]);
  });

  it("handles multilap via repeated marks (W-L two laps)", () => {
    const s = { lat: REF_LAT, lon: REF_LON };
    const wOff = offset(REF_LAT, REF_LON, 0, 2000);
    const w = { lat: wOff.lat, lon: wOff.lon };
    const course = [s, w, s, w, s];

    const legs = [
      lineThrough(s, 20, { t0: 0 }),
      lineThrough(w, 20, { t0: 300 }),
      lineThrough(s, 20, { t0: 600 }),
      lineThrough(w, 20, { t0: 900 }),
      lineThrough(s, 20, { t0: 1200 }),
    ];
    const track = legs.flat();
    const passes = computePasses(course, track);
    expect(passes.map((p) => p.markIndex)).toEqual([0, 1, 2, 3, 4]);
  });

  it("never completes a DNF track", () => {
    const aMark = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 2000);
    const bMark = { lat: bOff.lat, lon: bOff.lon };

    const det = new MarkRoundingDetector([aMark, bMark]);
    const passes = det.feedBatch(lineThrough(aMark, 20));

    expect(passes.map((p) => p.markIndex)).toEqual([0]);
    expect(det.nextMarkIndex).toBe(1);
    expect(det.done).toBe(false);
  });

  it("resumes from a persisted nextMarkIndex", () => {
    const aMark = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 2000);
    const bMark = { lat: bOff.lat, lon: bOff.lon };

    const det = new MarkRoundingDetector([aMark, bMark], { nextMarkIndex: 1 });
    const passes = det.feedBatch([
      ...lineThrough(aMark, 20, { t0: 100 }),
      ...lineThrough(bMark, 20, { t0: 300 }),
    ]);
    expect(passes.map((p) => p.markIndex)).toEqual([1]);
  });

  it("does not double-count dense jitter near CPA", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const track = lineThrough(mark, 5, { spanM: 400, n: 81 });
    expect(
      computePasses([mark], track, DEFAULT_INSHORE_THRESHOLD_M),
    ).toHaveLength(1);
  });

  it("emits the CPA timestamp, not the exit ts", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const distances = [60, 40, 20, 10, 5, 10, 20, 40, 60, 80, 100];
    const track = distances.map((d, i) => ({
      lat: REF_LAT,
      lon: REF_LON + mToDLon(d),
      ts: new Date(i * 1000).toISOString(),
    }));

    const passes = computePasses([mark], track, DEFAULT_INSHORE_THRESHOLD_M);
    expect(passes).toHaveLength(1);
    expect(passes[0].ts).toBe(track[4].ts);
    expect(passes[0].lon).toBe(track[4].lon);
  });

  it("rejects non-positive threshold", () => {
    expect(
      () =>
        new MarkRoundingDetector([{ lat: REF_LAT, lon: REF_LON }], {
          thresholdM: 0,
        }),
    ).toThrow();
  });

  it("radiusM kw still accepted (back-compat)", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const track = lineThrough(mark, 20);
    const det = new MarkRoundingDetector([mark], { radiusM: 100 });
    const passes = det.feedBatch(track);
    expect(passes).toHaveLength(1);
  });

  it("rejects mismatched per-mark threshold length", () => {
    const a = { lat: REF_LAT, lon: REF_LON };
    const b = { lat: REF_LAT + 0.01, lon: REF_LON };
    expect(
      () => new MarkRoundingDetector([a, b], { thresholdM: [100] }),
    ).toThrow();
    expect(
      () => new MarkRoundingDetector([a, b], { thresholdM: [100, 100, 100] }),
    ).toThrow();
  });

  it("thresholdsForCourse — distance mode", () => {
    expect(thresholdsForCourse(4, "distance")).toEqual([
      DEFAULT_DISTANCE_THRESHOLD_M,
      DEFAULT_DISTANCE_THRESHOLD_M,
      DEFAULT_DISTANCE_THRESHOLD_M,
      DEFAULT_DISTANCE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]);
  });

  it("thresholdsForCourse — inshore mode", () => {
    expect(thresholdsForCourse(3, "inshore")).toEqual([
      DEFAULT_INSHORE_THRESHOLD_M,
      DEFAULT_INSHORE_THRESHOLD_M,
      DEFAULT_INSHORE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]);
  });

  it("thresholdsForCourse — unknown mode defaults to distance", () => {
    expect(thresholdsForCourse(2, "nonsense")).toEqual([
      DEFAULT_DISTANCE_THRESHOLD_M,
      DEFAULT_DISTANCE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]);
  });

  it("thresholdsForCourse — edge cases", () => {
    expect(thresholdsForCourse(0)).toEqual([]);
    expect(thresholdsForCourse(1)).toEqual([
      DEFAULT_DISTANCE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]);
  });

  it("radiiForCourse aliases to distance-mode thresholds", () => {
    expect(radiiForCourse(3)).toEqual([
      DEFAULT_DISTANCE_THRESHOLD_M,
      DEFAULT_DISTANCE_THRESHOLD_M,
      DEFAULT_DISTANCE_THRESHOLD_M + FINAL_MARK_BONUS_M,
    ]);
  });
});
