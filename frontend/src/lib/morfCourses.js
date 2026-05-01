// MORF buoy courses from the 2026 Race Book.
// Every SA7 buoy course starts and finishes at SA7; only the middle marks differ.
//
// Long-distance courses (Zimmer, Skipper's Club, Hammond, etc.) aren't presets
// here yet — they need additional marks (R, D, C, Hammond Intake) and aren't
// the common case. Add as needed in a follow-up.

import { getMark } from "./morfMarks";

// Each entry: courseId → array of mark names that go between SA7 (start) and SA7 (finish).
const BUOY_COURSE_TABLE = {
  // Beer Trapezoid (T) — 4 rounding marks. Course Nautical Mileage: 5.40
  T1: ["1", "8", "6", "5"],
  T2: ["2", "1", "7", "6"],
  T3: ["3", "2", "8", "7"],
  T4: ["4", "3", "1", "8"],
  T5: ["5", "4", "2", "1"],
  T6: ["6", "5", "3", "2"],
  T7: ["7", "6", "4", "3"],
  T8: ["8", "7", "5", "4"],

  // Olympic (O) — 5 marks. Course Nautical Mileage: 9.62
  O1: ["1", "7", "5", "1", "5"],
  O2: ["2", "8", "6", "2", "6"],
  O3: ["3", "1", "7", "3", "7"],
  O4: ["4", "2", "8", "4", "8"],
  O5: ["5", "3", "1", "5", "1"],
  O6: ["6", "4", "2", "6", "2"],
  O7: ["7", "5", "3", "7", "3"],
  O8: ["8", "6", "4", "8", "4"],

  // Trapezoidal (P) — 6 marks. Course Nautical Mileage: 8.48
  P1: ["1", "8", "6", "8", "6", "5"],
  P2: ["2", "1", "7", "1", "7", "6"],
  P3: ["3", "2", "8", "2", "8", "7"],
  P4: ["4", "3", "1", "3", "1", "8"],
  P5: ["5", "4", "2", "4", "2", "1"],
  P6: ["6", "5", "3", "5", "3", "2"],
  P7: ["7", "6", "4", "6", "4", "3"],
  P8: ["8", "7", "5", "7", "5", "4"],

  // Circle (C) — 9 marks. Course Nautical Mileage: 8.90
  C1: ["1", "8", "7", "6", "5", "4", "3", "2", "1"],
  C2: ["2", "1", "8", "7", "6", "5", "4", "3", "2"],
  C3: ["3", "2", "1", "8", "7", "6", "5", "4", "3"],
  C4: ["4", "3", "2", "1", "8", "7", "6", "5", "4"],
  C5: ["5", "4", "3", "2", "1", "8", "7", "6", "5"],
  C6: ["6", "5", "4", "3", "2", "1", "8", "7", "6"],
  C7: ["7", "6", "5", "4", "3", "2", "1", "8", "7"],
  C8: ["8", "7", "6", "5", "4", "3", "2", "1", "8"],

  // Windward/Leeward Long (W) — 4 marks. Course Nautical Mileage: 8.72
  W1: ["1", "5", "1", "5"],
  W2: ["2", "6", "2", "6"],
  W3: ["3", "7", "3", "7"],
  W4: ["4", "8", "4", "8"],
  W5: ["5", "1", "5", "1"],
  W6: ["6", "2", "6", "2"],
  W7: ["7", "3", "7", "3"],
  W8: ["8", "4", "8", "4"],

  // Windward/Leeward (X) — 2 marks. Course Nautical Mileage: 6.54
  X1: ["1", "5"],
  X2: ["2", "6"],
  X3: ["3", "7"],
  X4: ["4", "8"],
  X5: ["5", "1"],
  X6: ["6", "2"],
  X7: ["7", "3"],
  X8: ["8", "4"],

  // Windward/Leeward Short (Y) — 2 marks. Course Statute Mileage: 4.36
  Y1: ["1", "5"],
  Y2: ["2", "6"],
  Y3: ["3", "7"],
  Y4: ["4", "8"],
  Y5: ["5", "1"],
  Y6: ["6", "2"],
  Y7: ["7", "3"],
  Y8: ["8", "4"],

  // Short W/L (S) — mark 2 is a gate at SA7 (or the R/C boat). For our purposes
  // we model it as a SA7 waypoint; the gate-rounding rules aren't part of the
  // route geometry. Course Nautical Mileage: 4.36
  S1: ["1", "SA7", "1"],
  S2: ["2", "SA7", "2"],
  S3: ["3", "SA7", "3"],
  S4: ["4", "SA7", "4"],
  S5: ["5", "SA7", "5"],
  S6: ["6", "SA7", "6"],
  S7: ["7", "SA7", "7"],
  S8: ["8", "SA7", "8"],
};

const FAMILY_LABELS = {
  T: "Beer Trapezoid (T) — 5.40 NM",
  O: "Olympic (O) — 9.62 NM",
  P: "Trapezoidal (P) — 8.48 NM",
  C: "Circle (C) — 8.90 NM",
  W: "Windward/Leeward Long (W) — 8.72 NM",
  X: "Windward/Leeward (X) — 6.54 NM",
  Y: "Windward/Leeward Short (Y) — 4.36 SM",
  S: "Short W/L (S) — 4.36 NM",
};

// Build the full mark list for a course id, including SA7 start and finish.
// Returns null if the course or any of its marks is unknown.
export function buildCourseMarks(courseId) {
  const middle = BUOY_COURSE_TABLE[courseId];
  if (!middle) return null;
  const sequence = ["SA7", ...middle, "SA7"];
  const marks = sequence.map((n) => getMark(n));
  if (marks.some((m) => m === null)) return null;
  return marks;
}

// Grouped option list for a <select> dropdown:
// [{ family: "T", label: "Beer Trapezoid (T) — 5.40 NM", courses: ["T1", ..., "T8"] }, ...]
export const COURSE_FAMILIES = Object.keys(FAMILY_LABELS).map((family) => ({
  family,
  label: FAMILY_LABELS[family],
  courses: Object.keys(BUOY_COURSE_TABLE).filter((id) => id.startsWith(family)),
}));

export const ALL_COURSE_IDS = Object.keys(BUOY_COURSE_TABLE);
