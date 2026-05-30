// latlon.test.js — regression tests for parseCoord.
//
// The original parseCoord regex used `[+-NSEW]` for the optional leading
// hemisphere letter. That's a stealth ASCII range from `+` (43) through
// `N` (78) which silently matches every digit; on input "41 52.80 N" the
// first `4` was consumed as the lead, leaving degStr="1" and the parsed
// value 1.88 instead of 41.88. The mark then landed in the South Pacific
// instead of Chicago.
//
// Runs under vitest on Windows/CI (vitest can't run in the cowork
// sandbox — see the project notes). Test stays in packages/shared so
// the same regression is guarded for both the webapp and the mobile app.

import { describe, expect, it } from "vitest";

import { parseCoord } from "./latlon.js";

describe("parseCoord", () => {
  it("parses deg-min with N suffix (regression: stealth range bug)", () => {
    // The case the user actually reported. Pre-fix returned 1.88.
    expect(parseCoord("41 52.80 N")).toBeCloseTo(41.88, 4);
  });

  it("parses three-digit longitudes with W suffix", () => {
    expect(parseCoord("087 34.46 W")).toBeCloseTo(-87.5743333, 4);
  });

  it("parses deg-min without hemisphere suffix", () => {
    expect(parseCoord("41 52.80")).toBeCloseTo(41.88, 4);
  });

  it("parses deg-min with ° separator", () => {
    expect(parseCoord("41°52.80 N")).toBeCloseTo(41.88, 4);
  });

  it("parses with leading hemisphere letter", () => {
    expect(parseCoord("N 41 52.80")).toBeCloseTo(41.88, 4);
    expect(parseCoord("S 41 52.80")).toBeCloseTo(-41.88, 4);
    expect(parseCoord("W 87 33.41")).toBeCloseTo(-87.5568333, 4);
  });

  it("parses decimal degrees with leading sign", () => {
    expect(parseCoord("+41.85283")).toBeCloseTo(41.85283, 5);
    expect(parseCoord("-87.55683")).toBeCloseTo(-87.55683, 5);
  });

  it("parses pure decimal degrees (no sign)", () => {
    expect(parseCoord("41.85283")).toBeCloseTo(41.85283, 5);
    expect(parseCoord("87.55683")).toBeCloseTo(87.55683, 5);
  });

  it("parses integer degrees (no decimal)", () => {
    expect(parseCoord("41")).toBe(41);
    expect(parseCoord("-87")).toBe(-87);
  });

  it("is case-insensitive on hemisphere letters", () => {
    expect(parseCoord("41 52.80 n")).toBeCloseTo(41.88, 4);
    expect(parseCoord("87 33.41 w")).toBeCloseTo(-87.5568333, 4);
  });

  it("returns NaN on empty / nullish / junk", () => {
    expect(parseCoord("")).toBeNaN();
    expect(parseCoord(null)).toBeNaN();
    expect(parseCoord(undefined)).toBeNaN();
    expect(parseCoord("not a coord")).toBeNaN();
    expect(parseCoord("41 abc N")).toBeNaN();
  });
});
