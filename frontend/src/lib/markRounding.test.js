// frontend/src/lib/markRounding.test.js
//
// Mirror of `backend/tests/test_mark_rounding.py`. Same scenarios so
// the JS port stays algorithmically identical to the Python source of
// truth.
//
// Geometry helpers are duplicated here rather than imported from the
// hook test files — the `markRounding` lib is the lowest tier and
// shouldn't depend on test plumbing elsewhere in the tree.

import { describe, it, expect } from "vitest";

import {
  computePasses,
  DEFAULT_RADIUS_M,
  haversineM,
  MarkRoundingDetector,
} from "./markRounding";

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
  { spanM = 200, n = 21, bearingDeg = 90, t0 = 0, dtS = 1 } = {},
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

describe("markRounding", () => {
  it("haversine round-trips a known distance", () => {
    // 1° latitude ≈ 111_000 m within ~1 km tolerance.
    const d = haversineM(0, 0, 1, 0);
    expect(d).toBeGreaterThan(110_000);
    expect(d).toBeLessThan(112_000);
  });

  it("DEFAULT_RADIUS_M matches the Python constant", () => {
    expect(DEFAULT_RADIUS_M).toBe(50);
  });

  it("emits one pass for a straight pass through the radius", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const track = lineThrough(mark, 10);
    const passes = computePasses([mark], track);
    expect(passes).toHaveLength(1);
    expect(passes[0].markIndex).toBe(0);
  });

  it("emits nothing for a fly-by outside the radius", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const track = lineThrough(mark, 75);
    expect(computePasses([mark], track)).toEqual([]);
  });

  it("ignores later marks crossed before earlier ones round", () => {
    const a = offset(REF_LAT, REF_LON, 0, 500);
    const aMark = { lat: a.lat, lon: a.lon };
    const bMark = { lat: REF_LAT, lon: REF_LON };

    const leg1 = lineThrough(bMark, 8, { t0: 0 }); // passes B (ignored)
    const leg2 = lineThrough(aMark, 8, { t0: 100 }); // rounds A
    const leg3 = lineThrough(bMark, 8, { t0: 200 }); // rounds B

    const passes = computePasses([aMark, bMark], [...leg1, ...leg2, ...leg3]);
    expect(passes.map((p) => p.markIndex)).toEqual([0, 1]);
  });

  it("handles multilap via repeated marks (W-L two laps)", () => {
    const s = { lat: REF_LAT, lon: REF_LON };
    const wOff = offset(REF_LAT, REF_LON, 0, 500);
    const w = { lat: wOff.lat, lon: wOff.lon };
    const course = [s, w, s, w, s];

    const legs = [
      lineThrough(s, 8, { t0: 0 }),
      lineThrough(w, 8, { t0: 120 }),
      lineThrough(s, 8, { t0: 240 }),
      lineThrough(w, 8, { t0: 360 }),
      lineThrough(s, 8, { t0: 480 }),
    ];
    const track = legs.flat();
    const passes = computePasses(course, track);
    expect(passes.map((p) => p.markIndex)).toEqual([0, 1, 2, 3, 4]);
  });

  it("never completes a DNF track", () => {
    const aMark = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 500);
    const bMark = { lat: bOff.lat, lon: bOff.lon };

    const det = new MarkRoundingDetector([aMark, bMark]);
    const passes = det.feedBatch(lineThrough(aMark, 8));

    expect(passes.map((p) => p.markIndex)).toEqual([0]);
    expect(det.nextMarkIndex).toBe(1);
    expect(det.done).toBe(false);
  });

  it("resumes from a persisted next_mark_index", () => {
    const aMark = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 500);
    const bMark = { lat: bOff.lat, lon: bOff.lon };

    const det = new MarkRoundingDetector([aMark, bMark], { nextMarkIndex: 1 });
    const passes = det.feedBatch([
      ...lineThrough(aMark, 8, { t0: 100 }),
      ...lineThrough(bMark, 8, { t0: 200 }),
    ]);
    expect(passes.map((p) => p.markIndex)).toEqual([1]);
  });

  it("does not double-count GPS jitter inside the radius", () => {
    const mark = { lat: REF_LAT, lon: REF_LON };
    const track = lineThrough(mark, 5, { spanM: 200, n: 41 });
    expect(computePasses([mark], track)).toHaveLength(1);
  });

  it("rejects non-positive radius", () => {
    expect(
      () => new MarkRoundingDetector([{ lat: REF_LAT, lon: REF_LON }], { radiusM: 0 }),
    ).toThrow();
  });
});
