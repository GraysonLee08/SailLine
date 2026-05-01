// Region registry — mirrors backend/app/regions.py.
//
// When you add a region, edit BOTH files. The names below are the public
// contract for /api/weather?region=... — they must match the backend.
//
// `defaultSource` is what the map requests by default for this region.
// Hawaii is GFS-only (HRRR doesn't cover it); everything else defaults to
// HRRR for higher resolution, with GFS available as a fallback.

export const REGIONS = {
  great_lakes: {
    name: "great_lakes",
    label: "Great Lakes",
    bbox: { minLat: 40.0, maxLat: 50.0, minLon: -94.0, maxLon: -75.0 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  chesapeake: {
    name: "chesapeake",
    label: "Chesapeake Bay",
    bbox: { minLat: 36.5, maxLat: 39.5, minLon: -77.5, maxLon: -75.5 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  long_island_sound: {
    name: "long_island_sound",
    label: "Long Island Sound",
    bbox: { minLat: 40.5, maxLat: 41.5, minLon: -74.0, maxLon: -71.5 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  new_england: {
    name: "new_england",
    label: "New England",
    bbox: { minLat: 40.5, maxLat: 43.5, minLon: -72.0, maxLon: -69.0 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  florida: {
    name: "florida",
    label: "South Florida",
    bbox: { minLat: 24.0, maxLat: 26.5, minLon: -82.0, maxLon: -79.5 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  gulf_coast: {
    name: "gulf_coast",
    label: "Gulf Coast",
    bbox: { minLat: 27.0, maxLat: 30.5, minLon: -94.0, maxLon: -82.0 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  socal: {
    name: "socal",
    label: "Southern California",
    bbox: { minLat: 32.5, maxLat: 34.5, minLon: -120.5, maxLon: -117.0 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  sf_bay: {
    name: "sf_bay",
    label: "San Francisco Bay",
    bbox: { minLat: 37.0, maxLat: 38.5, minLon: -123.5, maxLon: -121.5 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  pnw: {
    name: "pnw",
    label: "Pacific Northwest",
    bbox: { minLat: 47.0, maxLat: 49.0, minLon: -124.0, maxLon: -122.0 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  hawaii: {
    name: "hawaii",
    label: "Hawaii",
    bbox: { minLat: 18.5, maxLat: 22.5, minLon: -161.0, maxLon: -154.5 },
    sources: ["gfs"], // outside HRRR's CONUS domain
    defaultSource: "gfs",
  },
};

export const DEFAULT_REGION = "great_lakes";

/**
 * Find the region whose bbox contains (lat, lon). Returns the region object
 * or null if no region contains the point.
 */
export function regionFromPoint(lat, lon) {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  for (const region of Object.values(REGIONS)) {
    const { minLat, maxLat, minLon, maxLon } = region.bbox;
    if (lat >= minLat && lat <= maxLat && lon >= minLon && lon <= maxLon) {
      return region;
    }
  }
  return null;
}

/**
 * Center coordinate of a region — for `flyTo`. [lon, lat] order to match
 * Mapbox's expected input.
 */
export function regionCenter(region) {
  const { minLat, maxLat, minLon, maxLon } = region.bbox;
  return [(minLon + maxLon) / 2, (minLat + maxLat) / 2];
}

/**
 * Compute centroid of a list of marks (race course) and look up the
 * containing region. Used to override the user's home region when they
 * load a race that's elsewhere.
 *
 * Returns null if marks is empty/invalid or no region contains the centroid.
 */
export function regionFromMarks(marks) {
  if (!Array.isArray(marks) || marks.length === 0) return null;
  let latSum = 0;
  let lonSum = 0;
  let n = 0;
  for (const m of marks) {
    if (Number.isFinite(m?.lat) && Number.isFinite(m?.lon)) {
      latSum += m.lat;
      lonSum += m.lon;
      n += 1;
    }
  }
  if (n === 0) return null;
  return regionFromPoint(latSum / n, lonSum / n);
}

export function getRegion(name) {
  return REGIONS[name] || REGIONS[DEFAULT_REGION];
}
