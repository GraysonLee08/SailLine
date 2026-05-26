// formatRaceDate.test.js — pure-function tests.
//
// The helper uses Intl for weekday/month names, which respects the
// host locale. CI usually runs en-US, but to avoid making the tests
// locale-dependent we either:
//   (a) assert structural properties (contains a separator, has 5-char
//       HH:MM, year-suffix when relevant), or
//   (b) regex-match for the known en-US output where unambiguous.
// We use both — structural where locale-sensitive, exact where not.

import { describe, it, expect } from "vitest";
import { formatRaceDate } from "./formatRaceDate.js";

describe("formatRaceDate", () => {
  it("returns 'No start time' for null/undefined/empty", () => {
    expect(formatRaceDate(null)).toBe("No start time");
    expect(formatRaceDate(undefined)).toBe("No start time");
    expect(formatRaceDate("")).toBe("No start time");
  });

  it("returns 'Invalid date' for garbage input", () => {
    expect(formatRaceDate("not a date")).toBe("Invalid date");
    expect(formatRaceDate("2026-13-99T99:99:99Z")).toBe("Invalid date");
  });

  it("renders an in-year date without the year suffix", () => {
    // Pinned now = 2026-05-30 12:00 local. start_at in the same year.
    const now = new Date("2026-05-30T12:00:00");
    const result = formatRaceDate("2026-06-12T18:30:00", now);
    // Structural: contains the time (24h), separator, and no 4-digit
    // year token because it's the same year.
    expect(result).toMatch(/18:30/);
    expect(result).toMatch(/·/);
    expect(result).not.toMatch(/2026/);
  });

  it("includes the year for cross-year dates", () => {
    const now = new Date("2026-05-30T12:00:00");
    const result = formatRaceDate("2027-01-03T09:15:00", now);
    expect(result).toMatch(/09:15/);
    expect(result).toMatch(/2027/);
  });

  it("zero-pads the time", () => {
    const now = new Date("2026-05-30T12:00:00");
    const result = formatRaceDate("2026-06-12T04:05:00", now);
    expect(result).toMatch(/04:05/);
  });

  it("uses 24-hour time (not am/pm)", () => {
    const now = new Date("2026-05-30T12:00:00");
    const result = formatRaceDate("2026-06-12T22:30:00", now);
    expect(result).toMatch(/22:30/);
    expect(result.toLowerCase()).not.toMatch(/am|pm/);
  });
});
