// Region registry — mirrors backend/app/regions.py.
//
// Two kinds of regions:
//   - "base"  — always-on coverage. conus (HRRR @ 0.10° + GFS @ 0.25°) and
//               hawaii (GFS only). Frontend always loads one.
//   - "venue" — high-res HRRR overlay at native 0.027° (~3 km), one per
//               popular sailing area. Loaded only when zoom ≥ 11 AND
//               viewport center is inside the venue's bbox.
//
// When you add or rename a region, edit BOTH files. Names below are the
// public contract for /api/weather?region=... and must match the backend.

export const REGIONS = {
  // ── Base regions ──────────────────────────────────────────────────
  conus: {
    name: "conus",
    label: "Continental US",
    kind: "base",
    bbox: { minLat: 24.0, maxLat: 50.0, minLon: -126.0, maxLon: -66.0 },
    sources: ["hrrr", "gfs"],
    defaultSource: "hrrr",
  },
  hawaii: {
    name: "hawaii",
    label: "Hawaii",
    kind: "base",
    bbox: { minLat: 18.5, maxLat: 22.5, minLon: -161.0, maxLon: -154.5 },
    sources: ["gfs"], // outside HRRR's CONUS domain
    defaultSource: "gfs",
  },

  // ── Venues: Great Lakes ───────────────────────────────────────────
  chicago: {
    name: "chicago",
    label: "Chicago / Lake Michigan South",
    kind: "venue",
    bbox: { minLat: 41.6, maxLat: 42.5, minLon: -88.0, maxLon: -87.2 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  milwaukee: {
    name: "milwaukee",
    label: "Milwaukee Bay",
    kind: "venue",
    bbox: { minLat: 42.7, maxLat: 43.4, minLon: -88.1, maxLon: -87.5 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  detroit: {
    name: "detroit",
    label: "Lake St. Clair / Detroit",
    kind: "venue",
    bbox: { minLat: 41.9, maxLat: 43.0, minLon: -83.4, maxLon: -82.4 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  cleveland: {
    name: "cleveland",
    label: "Lake Erie Central",
    kind: "venue",
    bbox: { minLat: 41.4, maxLat: 42.3, minLon: -82.5, maxLon: -81.4 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },

  // ── Venues: West Coast ────────────────────────────────────────────
  sf_bay: {
    name: "sf_bay",
    label: "San Francisco Bay",
    kind: "venue",
    bbox: { minLat: 37.4, maxLat: 38.2, minLon: -122.6, maxLon: -121.9 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  long_beach: {
    name: "long_beach",
    label: "Long Beach / LA",
    kind: "venue",
    bbox: { minLat: 33.5, maxLat: 33.9, minLon: -118.4, maxLon: -117.9 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  san_diego: {
    name: "san_diego",
    label: "San Diego",
    kind: "venue",
    bbox: { minLat: 32.5, maxLat: 32.9, minLon: -117.4, maxLon: -117.0 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  puget_sound: {
    name: "puget_sound",
    label: "Puget Sound",
    kind: "venue",
    bbox: { minLat: 47.0, maxLat: 48.9, minLon: -122.8, maxLon: -122.3 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },

  // ── Venues: East Coast ────────────────────────────────────────────
  annapolis: {
    name: "annapolis",
    label: "Chesapeake / Annapolis",
    kind: "venue",
    bbox: { minLat: 38.7, maxLat: 39.3, minLon: -76.7, maxLon: -76.2 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  newport_ri: {
    name: "newport_ri",
    label: "Newport / Narragansett",
    kind: "venue",
    bbox: { minLat: 41.2, maxLat: 41.7, minLon: -71.6, maxLon: -71.0 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  buzzards_bay: {
    name: "buzzards_bay",
    label: "Buzzards Bay",
    kind: "venue",
    bbox: { minLat: 41.4, maxLat: 41.8, minLon: -71.2, maxLon: -70.6 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  marblehead: {
    name: "marblehead",
    label: "Marblehead / Boston",
    kind: "venue",
    bbox: { minLat: 42.3, maxLat: 42.7, minLon: -71.0, maxLon: -70.6 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  charleston: {
    name: "charleston",
    label: "Charleston",
    kind: "venue",
    bbox: { minLat: 32.5, maxLat: 32.9, minLon: -80.0, maxLon: -79.5 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },

  // ── Venues: Gulf / Florida ────────────────────────────────────────
  biscayne_bay: {
    name: "biscayne_bay",
    label: "Miami / Biscayne Bay",
    kind: "venue",
    bbox: { minLat: 25.4, maxLat: 25.9, minLon: -80.3, maxLon: -80.0 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
  corpus_christi: {
    name: "corpus_christi",
    label: "Corpus Christi",
    kind: "venue",
    bbox: { minLat: 27.5, maxLat: 27.9, minLon: -97.5, maxLon: -97.0 },
    sources: ["hrrr"],
    defaultSource: "hrrr",
  },
};

// Default base if GPS + IP geolocation both fail. CONUS is the safest
// because it covers ~95% of plausible US users.
export const DEFAULT_BASE_REGION = "conus";

// Zoom threshold at which the venue overlay activates. At zoom 11 the
// visible viewport is ~50 nm wide, which is the boundary between
// "passage planning" (CONUS-resolution is fine) and "tactical detail
// matters" (need native HRRR).
export const VENUE_ZOOM_THRESHOLD = 11;

const _baseRegions = () =>
  Object.values(REGIONS).filter((r) => r.kind === "base");
const _venues = () => Object.values(REGIONS).filter((r) => r.kind === "venue");

function _contains(region, lat, lon) {
  const { minLat, maxLat, minLon, maxLon } = region.bbox;
  return lat >= minLat && lat <= maxLat && lon >= minLon && lon <= maxLon;
}

/** Find the base region (conus or hawaii) containing this point. Null if neither. */
export function baseRegionForPoint(lat, lon) {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  for (const r of _baseRegions()) if (_contains(r, lat, lon)) return r;
  return null;
}

/** Find the venue (if any) containing this point. */
export function venueForPoint(lat, lon) {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  for (const r of _venues()) if (_contains(r, lat, lon)) return r;
  return null;
}

/** Centroid of an array of marks, or null if invalid/empty. */
export function marksCentroid(marks) {
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
  return n === 0 ? null : { lat: latSum / n, lon: lonSum / n };
}

/** Center [lon, lat] of a region — for Mapbox's flyTo. */
export function regionCenter(region) {
  const { minLat, maxLat, minLon, maxLon } = region.bbox;
  return [(minLon + maxLon) / 2, (minLat + maxLat) / 2];
}

export function getRegion(name) {
  return REGIONS[name] || REGIONS[DEFAULT_BASE_REGION];
}
