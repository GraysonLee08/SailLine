// Wind barb SVG generation — modern, clean styling.
// Generates one icon per 5kt bucket; Mapbox rotates it via icon-rotate.
//
// Also: bilinear interpolation helpers for displaying barbs at finer
// spacing than the native grid resolution. Source data is HRRR regridded
// to ~0.1° (~11km at our latitudes); when zoomed past ~zoom 10 a
// race-area-sized viewport contains 0–1 native points, so we interpolate
// to keep the visualization useful. Interpolated values are smoother but
// add no information beyond the native resolution.

const COLOR = "#1f2937";       // slate-800: muted, modern, reads well on light maps
const CALM_COLOR = "#94a3b8";  // slate-400: subdued for calm conditions
const STROKE = 1.5;

/**
 * Convert u/v wind components (m/s, meteorological convention) to speed (knots)
 * and the "from" direction in degrees clockwise from north.
 *
 *   u > 0  → wind blowing toward the east
 *   v > 0  → wind blowing toward the north
 *
 * Direction returned is where the wind is COMING FROM, which is what wind
 * barbs render: shaft points toward the source.
 */
export function uvToSpeedDir(u, v) {
  const speedMs = Math.hypot(u, v);
  const speedKt = speedMs * 1.94384;
  const dirRad = Math.atan2(-u, -v);
  const dirDeg = ((dirRad * 180) / Math.PI + 360) % 360;
  return { speedKt, dirDeg };
}

/**
 * Find index `i` in a monotonic array `arr` such that `target` falls
 * between arr[i] and arr[i+1] (inclusive on the lower side). Handles both
 * ascending and descending arrays — HRRR lats can come either direction.
 *
 * Returns -1 if `target` is outside the array's range.
 */
function findBracketIndex(arr, target) {
  const n = arr.length;
  if (n < 2) return -1;
  const ascending = arr[n - 1] > arr[0];

  if (ascending) {
    if (target < arr[0] || target > arr[n - 1]) return -1;
    let lo = 0;
    let hi = n - 1;
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1;
      if (arr[mid] <= target) lo = mid;
      else hi = mid;
    }
    return lo;
  } else {
    if (target > arr[0] || target < arr[n - 1]) return -1;
    let lo = 0;
    let hi = n - 1;
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1;
      if (arr[mid] >= target) lo = mid;
      else hi = mid;
    }
    return lo;
  }
}

/**
 * Bilinear interpolation of (u, v) at an arbitrary (lat, lon) from a
 * regular lat/lon grid. Returns null if the point is outside the grid.
 *
 * @param {{lats: number[], lons: number[], u: number[][], v: number[][]}} weather
 * @param {number} lat
 * @param {number} lon
 * @returns {{u: number, v: number} | null}
 */
export function bilerpUV(weather, lat, lon) {
  const { lats, lons, u, v } = weather;

  const i = findBracketIndex(lats, lat);
  const j = findBracketIndex(lons, lon);
  if (i < 0 || j < 0) return null;

  const lat0 = lats[i];
  const lat1 = lats[i + 1];
  const lon0 = lons[j];
  const lon1 = lons[j + 1];

  // Normalized fractional position inside the cell. Works for both
  // ascending and descending coord arrays because (target - lat0) and
  // (lat1 - lat0) flip sign together.
  const fy = (lat - lat0) / (lat1 - lat0);
  const fx = (lon - lon0) / (lon1 - lon0);

  const u00 = u[i][j];
  const u01 = u[i][j + 1];
  const u10 = u[i + 1][j];
  const u11 = u[i + 1][j + 1];
  const v00 = v[i][j];
  const v01 = v[i][j + 1];
  const v10 = v[i + 1][j];
  const v11 = v[i + 1][j + 1];

  const w00 = (1 - fx) * (1 - fy);
  const w01 = fx * (1 - fy);
  const w10 = (1 - fx) * fy;
  const w11 = fx * fy;

  return {
    u: w00 * u00 + w01 * u01 + w10 * u10 + w11 * u11,
    v: w00 * v00 + w01 * v01 + w10 * v10 + w11 * v11,
  };
}

/**
 * Build a wind barb SVG (as a data URL) for a given speed bucket.
 * Drawn pointing UP (north) in unrotated form. Flags are on the LEFT of
 * the shaft per Northern Hemisphere convention.
 */
function makeBarbSVG(bucketKt) {
  const SIZE = 64;
  const CX = SIZE / 2;
  const CY = SIZE / 2;
  const SHAFT_LEN = 26;
  const SHAFT_TOP = CY - SHAFT_LEN;
  const FLAG_LEN = 11;
  const FLAG_GAP = 4;

  // Calm: small open circle, no shaft.
  if (bucketKt < 5) {
    const inner =
      `<circle cx="${CX}" cy="${CY}" r="3" ` +
      `stroke="${CALM_COLOR}" stroke-width="${STROKE}" fill="none"/>`;
    return wrap(SIZE, inner);
  }

  // Decompose speed into pennants (50kt) / full flags (10kt) / half flags (5kt).
  let remaining = bucketKt;
  const pennants = Math.floor(remaining / 50);
  remaining -= pennants * 50;
  const fullFlags = Math.floor(remaining / 10);
  remaining -= fullFlags * 10;
  const halfFlags = remaining >= 5 ? 1 : 0;

  const parts = [
    // Shaft.
    `<line x1="${CX}" y1="${CY}" x2="${CX}" y2="${SHAFT_TOP}" ` +
      `stroke="${COLOR}" stroke-width="${STROKE}" stroke-linecap="round"/>`,
    // Anchor dot at the station.
    `<circle cx="${CX}" cy="${CY}" r="1.8" fill="${COLOR}"/>`,
  ];

  let y = SHAFT_TOP;

  // Pennants: filled triangles on the LEFT of the shaft.
  for (let i = 0; i < pennants; i++) {
    parts.push(
      `<polygon points="${CX},${y} ${CX - FLAG_LEN},${y + 2} ${CX},${y + 7}" ` +
        `fill="${COLOR}"/>`
    );
    y += 8;
  }
  if (pennants > 0) y += 2;

  // Full flags: angle up-left from the shaft.
  for (let i = 0; i < fullFlags; i++) {
    parts.push(
      `<line x1="${CX}" y1="${y}" x2="${CX - FLAG_LEN}" y2="${y - 4}" ` +
        `stroke="${COLOR}" stroke-width="${STROKE}" stroke-linecap="round"/>`
    );
    y += FLAG_GAP;
  }

  // Half flag: shorter, conventionally offset from the shaft tip when alone.
  if (halfFlags > 0) {
    if (fullFlags === 0 && pennants === 0) y += FLAG_GAP;
    parts.push(
      `<line x1="${CX}" y1="${y}" x2="${CX - FLAG_LEN * 0.5}" y2="${y - 2}" ` +
        `stroke="${COLOR}" stroke-width="${STROKE}" stroke-linecap="round"/>`
    );
  }

  return wrap(SIZE, parts.join(""));
}

function wrap(size, inner) {
  const xml =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" ` +
    `viewBox="0 0 ${size} ${size}">${inner}</svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(xml)}`;
}

/**
 * Pre-generate all barb images, keyed by speed bucket (0, 5, 10, ..., 65 kt).
 * Mapbox loads each once at startup, then symbol layer references them by id.
 */
export function generateBarbImages() {
  const images = {};
  for (let kt = 0; kt <= 65; kt += 5) {
    images[`barb-${kt}`] = makeBarbSVG(kt);
  }
  return images;
}
