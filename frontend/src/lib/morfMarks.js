// Named MORF marks from the 2026 Race Book Table 8.
// All coordinates are stored as decimal degrees (signed: West/South negative).
// Source values in the race book are degrees + decimal minutes.

const dm = (deg, min, sign = 1) => sign * (deg + min / 60);
const W = -1; // West longitudes

export const MORF_MARKS = {
  SA7: { name: "SA7", lat: dm(41, 51.17), lon: dm(87, 33.41, W), description: "205° - 1.3 miles from Four Mile Crib (MORF starting area)" },
  "1": { name: "1", lat: dm(41, 52.26), lon: dm(87, 33.41, W), description: "360° - 1.09 miles from SA7" },
  "2": { name: "2", lat: dm(41, 51.94), lon: dm(87, 32.37, W), description: "045° - 1.09 miles from SA7" },
  "3": { name: "3", lat: dm(41, 51.17), lon: dm(87, 31.95, W), description: "090° - 1.09 miles from SA7" },
  "4": { name: "4", lat: dm(41, 50.40), lon: dm(87, 32.37, W), description: "135° - 1.09 miles from SA7" },
  "5": { name: "5", lat: dm(41, 50.08), lon: dm(87, 33.41, W), description: "180° - 1.09 miles from SA7" },
  "6": { name: "6", lat: dm(41, 50.40), lon: dm(87, 34.44, W), description: "225° - 1.09 miles from SA7" },
  "7": { name: "7", lat: dm(41, 51.17), lon: dm(87, 34.87, W), description: "270° - 1.09 miles from SA7" },
  "8": { name: "8", lat: dm(41, 51.94), lon: dm(87, 34.44, W), description: "315° - 1.09 miles from SA7" },
  JP: { name: "JP", lat: dm(41, 47.10), lon: dm(87, 34.10, W), description: "Outer East End of Jackson Park Break Wall" },
  "4-Mile Crib": { name: "4-Mile Crib", lat: dm(41, 52.38), lon: dm(87, 32.75, W), description: "4 Mile Crib" },
  "68 St Crib": { name: "68 St Crib", lat: dm(41, 47.18), lon: dm(87, 31.88, W), description: "68 St. Crib" },
  "Wm Dever Crib": { name: "Wm Dever Crib", lat: dm(41, 54.99), lon: dm(87, 34.33, W), description: "William Dever Crib" },
  "Wilson Ave Crib": { name: "Wilson Ave Crib", lat: dm(41, 58.00), lon: dm(87, 35.50, W), description: "Wilson Ave. Crib" },
  "Hammond Intake Crib": { name: "Hammond Intake Crib", lat: dm(41, 42.15), lon: dm(87, 29.49, W), description: "Hammond Intake Crib" },
  R: { name: "R", lat: dm(41, 45.60), lon: dm(87, 28.03, W), description: "Northeast Shoal Lighted Buoy (Light List #19790)" },
  D: { name: "D", lat: dm(41, 46.17), lon: dm(87, 23.45, W), description: "Indiana Shoal Buoy #2 (Light List #19785)" },
  C: { name: "C", lat: dm(41, 48.41), lon: dm(87, 32.08, W), description: "Clemson Shoal Lighted Bell (Light List #19910)" },
  WR2: { name: "WR2", lat: dm(42, 5.69), lon: dm(87, 38.96, W), description: "Wilmette Wreck Lighted Bell Buoy WR2" },
  WFM: { name: "WFM", lat: dm(42, 21.70), lon: dm(87, 47.90, W), description: "Waukegan Finishing Mark, 0.5 NM East of Waukegan Lighthouse Pier" },
  SA1: { name: "SA1", lat: dm(41, 57.74), lon: dm(87, 36.40, W), description: "Center, CCYC Circle, 1 mile east of Montrose Harbor Point" },
  SA2: { name: "SA2", lat: dm(41, 56.50), lon: dm(87, 36.85, W), description: "Approximately 0.9 NM East Belmont Harbor Light" },
  SA3: { name: "SA3", lat: dm(41, 54.33), lon: dm(87, 32.38, W), description: "Approximately 2.3 NM North/Northeast of the 4 Mile Crib" },
  SA4: { name: "SA4", lat: dm(41, 52.30), lon: dm(87, 35.20, W), description: "Approximately 2 NM East of Buckingham Fountain" },
};

// Ordered list for picker UIs. SA7 first since it's the start/finish for every buoy course.
export const MORF_MARK_LIST = [
  "SA7", "1", "2", "3", "4", "5", "6", "7", "8",
  "JP", "4-Mile Crib", "68 St Crib", "Wm Dever Crib", "Wilson Ave Crib", "Hammond Intake Crib",
  "R", "D", "C", "WR2", "WFM", "SA1", "SA2", "SA3", "SA4",
].map((k) => MORF_MARKS[k]);

// Returns a fresh copy so callers can't mutate the library.
export function getMark(name) {
  const m = MORF_MARKS[name];
  return m ? { ...m } : null;
}
