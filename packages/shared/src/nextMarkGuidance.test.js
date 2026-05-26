// packages/shared/src/nextMarkGuidance.test.js
//
// Pure tests for the live next-mark guidance helpers. Vitest, run via
// the frontend's npm test (the workspace test runner picks up
// packages/shared/**/*.test.js).

import { describe, it, expect } from "vitest";

import {
  computeGuidance,
  crossTrackErrorM,
  initialBearingDeg,
} from "./nextMarkGuidance.js";
import { FINAL_MARK_RADIUS_M } from "./markRounding.js";

// ── initialBearingDeg ──────────────────────────────────────────────────

describe("initialBearingDeg", () => {
  it("points due north when destination is due north", () => {
    const b = initialBearingDeg(40, -87, 41, -87);
    expect(b).toBeCloseTo(0, 1);
  });

  it("points due east at the equator when destination is due east", () => {
    const b = initialBearingDeg(0, 0, 0, 1);
    expect(b).toBeCloseTo(90, 1);
  });

  it("points due south when destination is due south", () => {
    const b = initialBearingDeg(40, -87, 39, -87);
    expect(b).toBeCloseTo(180, 1);
  });

  it("points due west at the equator when destination is due west", () => {
    const b = initialBearingDeg(0, 0, 0, -1);
    expect(b).toBeCloseTo(270, 1);
  });

  it("returns a value in [0, 360)", () => {
    const b = initialBearingDeg(40, -87, 40.001, -87.001);
    expect(b).toBeGreaterThanOrEqual(0);
    expect(b).toBeLessThan(360);
  });
});

// ── crossTrackErrorM ───────────────────────────────────────────────────

describe("crossTrackErrorM", () => {
  it("is zero on the line", () => {
    // Line from (40, -87) due north to (41, -87). Midpoint is on the line.
    const d = crossTrackErrorM(40, -87, 41, -87, 40.5, -87);
    expect(Math.abs(d)).toBeLessThan(1);
  });

  it("is positive when point is to the right (east) of a northbound line", () => {
    const d = crossTrackErrorM(40, -87, 41, -87, 40.5, -86.9);
    expect(d).toBeGreaterThan(0);
  });

  it("is negative when point is to the left (west) of a northbound line", () => {
    const d = crossTrackErrorM(40, -87, 41, -87, 40.5, -87.1);
    expect(d).toBeLessThan(0);
  });

  it("magnitude grows with offset", () => {
    const d1 = Math.abs(crossTrackErrorM(40, -87, 41, -87, 40.5, -86.99));
    const d2 = Math.abs(crossTrackErrorM(40, -87, 41, -87, 40.5, -86.9));
    expect(d2).toBeGreaterThan(d1);
  });
});

// ── computeGuidance ────────────────────────────────────────────────────

const MARKS = [
  { name: "A", lat: 41.900, lon: -87.600 },  // first mark
  { name: "B", lat: 41.910, lon: -87.600 },  // upwind
  { name: "C", lat: 41.900, lon: -87.580 },  // gybe
  { name: "Finish", lat: 41.900, lon: -87.600 }, // back to start
];

describe("computeGuidance", () => {
  it("returns null when there is no next mark (race finished)", () => {
    // Force-replay all marks by feeding tight in/out points around each.
    const points = [];
    for (const m of MARKS) {
      // approach from the south, exit to the north (within 10m of the mark)
      points.push({ lat: m.lat - 0.001, lon: m.lon, ts: "2026-05-26T00:00:00Z" });
      points.push({ lat: m.lat, lon: m.lon, ts: "2026-05-26T00:00:01Z" });
      points.push({ lat: m.lat + 0.001, lon: m.lon, ts: "2026-05-26T00:00:02Z" });
    }
    const result = computeGuidance({
      marks: MARKS,
      points,
      current: { lat: 41.911, lon: -87.600 },
      radiusM: FINAL_MARK_RADIUS_M, // wide enough for all marks in this test
    });
    expect(result).toBeNull();
  });

  it("identifies mark A as next when no points have been recorded", () => {
    const result = computeGuidance({
      marks: MARKS,
      points: [],
      current: { lat: 41.890, lon: -87.600 },
    });
    expect(result).not.toBeNull();
    expect(result.nextMarkIndex).toBe(0);
    expect(result.nextMark.name).toBe("A");
  });

  it("reports the correct bearing for a due-north next mark", () => {
    const result = computeGuidance({
      marks: MARKS,
      points: [],
      current: { lat: 41.890, lon: -87.600 },
    });
    expect(result.bearingDeg).toBeCloseTo(0, 0);
  });

  it("reports increasing distance as we get farther away", () => {
    const close = computeGuidance({
      marks: MARKS,
      points: [],
      current: { lat: 41.899, lon: -87.600 },
    });
    const far = computeGuidance({
      marks: MARKS,
      points: [],
      current: { lat: 41.880, lon: -87.600 },
    });
    expect(far.distanceM).toBeGreaterThan(close.distanceM);
  });

  it("reports zero cross-track on the first leg (no previous mark)", () => {
    const result = computeGuidance({
      marks: MARKS,
      points: [],
      current: { lat: 41.895, lon: -87.605 },
    });
    expect(result.crossTrackM).toBe(0);
    expect(result.fromMark).toBeNull();
  });

  it("computes a non-zero cross-track on the second leg", () => {
    // Round mark A first.
    const points = [
      { lat: 41.899, lon: -87.600, ts: "2026-05-26T00:00:00Z" },
      { lat: 41.900, lon: -87.600, ts: "2026-05-26T00:00:01Z" }, // inside
      { lat: 41.9005, lon: -87.600, ts: "2026-05-26T00:00:02Z" }, // exit
    ];
    // Now sailing toward B; current point offset to the east of the line.
    const result = computeGuidance({
      marks: MARKS,
      points,
      current: { lat: 41.905, lon: -87.5995 },
    });
    expect(result.nextMarkIndex).toBe(1);
    expect(result.fromMark?.lat).toBeCloseTo(41.900, 3);
    expect(result.crossTrackM).not.toBe(0);
  });

  it("returns null for empty marks", () => {
    expect(
      computeGuidance({
        marks: [],
        points: [],
        current: { lat: 41.9, lon: -87.6 },
      }),
    ).toBeNull();
  });

  it("returns null for invalid current point", () => {
    expect(
      computeGuidance({
        marks: MARKS,
        points: [],
        current: { lat: NaN, lon: -87.6 },
      }),
    ).toBeNull();
  });
});
