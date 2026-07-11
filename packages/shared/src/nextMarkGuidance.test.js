// packages/shared/src/nextMarkGuidance.test.js
//
// Pure tests for the live next-mark guidance helpers. Vitest, run via
// the frontend's npm test (the workspace test runner picks up
// packages/shared/**/*.test.js).
//
// v3 detector note (2026-05-30, commit adf2c97): `MarkRoundingDetector`
// switched from fixed-radius enter/exit to streaming sequential CPA.
// A pass now fires when (a) the running minimum-distance to the mark
// is at or below threshold AND (b) DEPART_CONFIRM_SAMPLES (=3)
// strictly-increasing samples follow the CPA. The pre-v3 pattern of
// "one point south + one point on + one point north" doesn't trigger
// the detector because it lacks enough strictly-increasing samples
// after the CPA. Tests that need to simulate a rounding use the
// `passMark()` helper below to generate a CPA-with-three-departures
// pattern per mark.

import { describe, it, expect } from "vitest";

import {
  computeGuidance,
  crossTrackErrorM,
  initialBearingDeg,
} from "./nextMarkGuidance.js";
import { FINAL_MARK_RADIUS_M } from "./markRounding.js";

/**
 * Generate a point sequence that fires the v3 detector for one mark:
 * a CPA sample on the mark, followed by three strictly-increasing
 * northward samples (departing the mark). Returns 4 points starting
 * at offset `tOffsetS` seconds.
 *
 * Samples step 55m → 110m → 220m north of the mark after the CPA, all
 * well inside the FINAL_MARK_RADIUS_M (=150m) threshold's CPA window
 * (CPA distance is 0). The next mark in the course is far enough away
 * that the chained-feed-after-emit doesn't accidentally count these
 * departing samples toward the next mark's traversal.
 */
function passMark(mark, tOffsetS) {
  return [
    { lat: mark.lat,           lon: mark.lon, ts: new Date((tOffsetS + 0) * 1000).toISOString() }, // CPA
    { lat: mark.lat + 0.0005,  lon: mark.lon, ts: new Date((tOffsetS + 1) * 1000).toISOString() }, // dep1 ≈ +55 m
    { lat: mark.lat + 0.0010,  lon: mark.lon, ts: new Date((tOffsetS + 2) * 1000).toISOString() }, // dep2 ≈ +111 m
    { lat: mark.lat + 0.0020,  lon: mark.lon, ts: new Date((tOffsetS + 3) * 1000).toISOString() }, // dep3 ≈ +222 m → emit
  ];
}

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
    // Round every mark by replaying the v3 detector's CPA + departure
    // pattern at each. Each mark contributes 4 points (CPA + 3
    // strictly-increasing departures); the chained emit drains the
    // pass before the next mark's pattern starts.
    const points = [];
    MARKS.forEach((m, i) => {
      points.push(...passMark(m, i * 100));
    });
    const result = computeGuidance({
      marks: MARKS,
      points,
      current: { lat: 41.911, lon: -87.600 },
      radiusM: FINAL_MARK_RADIUS_M, // 150 m — CPA of 0 m is well inside
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
    // Round mark A with the v3 detector's CPA + 3 departing pattern.
    // The departing samples stay 222 m north of A (still ~890 m south
    // of B), so they don't accidentally round B too.
    const points = passMark(MARKS[0], 0);
    // Now sailing toward B; current point offset slightly east of the
    // A→B line.
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

// ── computeGuidance: server-authoritative nextMarkIndex (2026-07-10) ──
//
// When the caller supplies the persisted mark_passes count, the local
// detector replay must be skipped entirely — a truncated point window
// regresses to already-passed marks (the Last Test "next mark: Start"
// bug). These pin the override semantics.

describe("computeGuidance with nextMarkIndex", () => {
  it("uses the server index and ignores the points entirely", () => {
    // Points would replay to "A is next" (no passes) — but the server
    // says two marks are already passed. Server wins.
    const result = computeGuidance({
      marks: MARKS,
      points: [],
      current: { lat: 41.905, lon: -87.590 },
      nextMarkIndex: 2,
    });
    expect(result).not.toBeNull();
    expect(result.nextMarkIndex).toBe(2);
    expect(result.nextMark.name).toBe("C");
    // Leg line is B→C, so fromMark must be B.
    expect(result.fromMark?.lat).toBeCloseTo(41.910, 3);
  });

  it("does NOT regress when points cover only a recent window", () => {
    // Reproduces the 2026-07-10 bug shape: the point window contains
    // no rounding patterns at all (they scrolled out), which the old
    // replay path read as "next mark index 0". With the server index
    // the answer stays monotonic.
    const staleWindow = [
      { lat: 41.905, lon: -87.590, ts: "2026-07-10T23:18:00Z" },
      { lat: 41.9051, lon: -87.5899, ts: "2026-07-10T23:18:01Z" },
    ];
    const result = computeGuidance({
      marks: MARKS,
      points: staleWindow,
      current: { lat: 41.9051, lon: -87.5899 },
      nextMarkIndex: 3,
    });
    expect(result.nextMarkIndex).toBe(3);
    expect(result.nextMark.name).toBe("Finish");
  });

  it("returns null when the server says the course is complete", () => {
    const result = computeGuidance({
      marks: MARKS,
      points: [],
      current: { lat: 41.9, lon: -87.6 },
      nextMarkIndex: MARKS.length,
    });
    expect(result).toBeNull();
  });

  it("falls back to the replay path for null / undefined / bogus index", () => {
    for (const bogus of [null, undefined, -1, 1.5, "2", NaN]) {
      const result = computeGuidance({
        marks: MARKS,
        points: [],
        current: { lat: 41.890, lon: -87.600 },
        nextMarkIndex: bogus,
      });
      expect(result).not.toBeNull();
      expect(result.nextMarkIndex).toBe(0); // replay of empty points
    }
  });
});
