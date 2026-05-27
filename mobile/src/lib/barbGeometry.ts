// lib/barbGeometry.ts — pre-compute wind-barb LineString features.
//
// Builds true meteorological wind barbs as GeoJSON LineString features
// suitable for a single Mapbox LineLayer call. Each barb is composed of:
//   * A shaft from the station point in the "from" direction (upwind).
//   * Full flags (10 kt each) as short lines branching off the shaft.
//   * Half flags (5 kt) as half-length lines.
//   * Pennants (50 kt) as triangular polygons (returned as 3-point
//     closed LineStrings — Mapbox LineLayer renders them as triangles
//     because of the closed path; visually identical to a filled
//     pennant at the small scale we draw at).
//
// Why pre-compute in JS rather than letting Mapbox style expressions
// do the work: barb glyph composition requires per-feature variable
// geometry (the number of flags depends on speed), and Mapbox style
// expressions can't generate per-feature LineString variations. The
// alternative — one icon per speed bucket via SymbolLayer — fails on
// Android because the native bitmap loader can't decode SVG data URLs.
//
// Coordinates are in lat/lon (WGS84). For the small barb size (~20m
// per flag at typical zoom), we can treat lat/lon as locally Euclidean
// without measurable distortion. Sub-pixel error at any practical
// barb size.

const METRES_PER_DEG_LAT = 111_320; // constant enough

// Flag/shaft sizes in METRES. Tuned so a barb is ~legible at zoom 11+
// (~50 m/px). At higher zoom they look proportionally larger; at lower
// zoom they shrink, which is fine — barbs aren't useful when so zoomed
// out that you can't see the marks anyway.
const SHAFT_LEN_M = 800;
const FULL_FLAG_LEN_M = 320;
const HALF_FLAG_LEN_M = 160;
const FLAG_OFFSET_M = 80;   // gap between successive flags along the shaft
const PENNANT_LEN_M = 320;
const PENNANT_WIDTH_M = 200;

type BarbInput = {
  /** Station coordinates (where the barb originates). */
  lat: number;
  lon: number;
  /** Wind speed in knots. Negative or NaN treated as 0. */
  speedKt: number;
  /** Direction wind is FROM, degrees clockwise from true north. */
  dirDeg: number;
};

type BarbFeature = {
  type: "Feature";
  geometry: {
    type: "LineString";
    coordinates: [number, number][];
  };
  properties: {
    bucket: number; // speed bucket for color stepping in the LineLayer
    kind: "shaft" | "flag" | "pennant";
  };
};

/**
 * Offset `(lat, lon)` by `(dx_m, dy_m)` in metres, where dx is east
 * and dy is north. Locally Euclidean approximation — good to sub-pixel
 * at the scales we draw barbs.
 */
function offsetMetres(
  lat: number,
  lon: number,
  dxEastM: number,
  dyNorthM: number,
): [number, number] {
  const metresPerDegLon = METRES_PER_DEG_LAT * Math.cos((lat * Math.PI) / 180);
  return [lon + dxEastM / metresPerDegLon, lat + dyNorthM / METRES_PER_DEG_LAT];
}

/**
 * Build all LineString features for one barb. Returns an array of 0..N
 * features (calm winds return an empty array — render a separate dot
 * marker for those if desired).
 *
 * Coordinate system note: meteorological direction is "wind comes
 * FROM this bearing", measured clockwise from true north. The barb
 * SHAFT points TOWARD the source — i.e., upwind. So a wind from 90°
 * (east) gets a shaft pointing east from the station.
 *
 * Flags branch off the LEFT side of the shaft (Northern Hemisphere
 * convention), which in our coordinate system means rotating the
 * shaft vector 90° COUNTERCLOCKWISE to get the flag direction.
 */
export function buildBarbFeatures(input: BarbInput): BarbFeature[] {
  const speedKt = Math.max(0, Math.floor(input.speedKt));
  if (!Number.isFinite(speedKt) || speedKt < 5) return [];

  // Decompose speed into pennants (50), full flags (10), half flag (5).
  let remaining = speedKt;
  const pennants = Math.floor(remaining / 50);
  remaining -= pennants * 50;
  const fullFlags = Math.floor(remaining / 10);
  remaining -= fullFlags * 10;
  const halfFlags = remaining >= 5 ? 1 : 0;

  // Bucket label for color stepping (mirror the 5kt buckets used elsewhere).
  const bucket = Math.min(Math.round(speedKt / 5) * 5, 65);

  // Direction vector components (unit): wind FROM dir means shaft
  // points TOWARD dir. dir=0 (from north) → shaft tip points north.
  const dirRad = (input.dirDeg * Math.PI) / 180;
  const dxUnit = Math.sin(dirRad);   // east component
  const dyUnit = Math.cos(dirRad);   // north component
  // Left-perpendicular (rotate the dir vector 90° CCW): used for flag
  // branching direction (Northern-Hemisphere barb convention).
  const lxUnit = -dyUnit;
  const lyUnit = dxUnit;

  const features: BarbFeature[] = [];

  // 1) Shaft. From station out to (shaftLen) along dirVec.
  const shaftTipLon = input.lon + (dxUnit * SHAFT_LEN_M) /
    (METRES_PER_DEG_LAT * Math.cos((input.lat * Math.PI) / 180));
  const shaftTipLat = input.lat + (dyUnit * SHAFT_LEN_M) / METRES_PER_DEG_LAT;
  features.push({
    type: "Feature",
    geometry: {
      type: "LineString",
      coordinates: [[input.lon, input.lat], [shaftTipLon, shaftTipLat]],
    },
    properties: { bucket, kind: "shaft" },
  });

  // Walk down the shaft FROM the tip back TOWARD the station, placing
  // flags. Pennants first (largest, at the tip end), then full flags,
  // then half flags. This matches the conventional barb layout.
  // Position is parameterised by distance from station along shaft.
  let posM = SHAFT_LEN_M;

  // 2) Pennants — filled triangles. We emit them as closed 4-point
  //    LineStrings; the LineLayer renders them as outlined triangles
  //    which read as filled at our line widths.
  for (let i = 0; i < pennants; i++) {
    const baseM = posM;
    const tipM = posM - PENNANT_LEN_M;
    posM -= PENNANT_LEN_M + FLAG_OFFSET_M;

    const baseShaft = offsetMetres(input.lat, input.lon, dxUnit * baseM, dyUnit * baseM);
    const tipShaft = offsetMetres(input.lat, input.lon, dxUnit * tipM, dyUnit * tipM);
    // Flag flares to the LEFT perpendicular.
    const baseFlag = offsetMetres(
      input.lat, input.lon,
      dxUnit * baseM + lxUnit * PENNANT_WIDTH_M,
      dyUnit * baseM + lyUnit * PENNANT_WIDTH_M,
    );
    features.push({
      type: "Feature",
      geometry: {
        type: "LineString",
        coordinates: [baseShaft, baseFlag, tipShaft, baseShaft],
      },
      properties: { bucket, kind: "pennant" },
    });
  }

  // 3) Full flags — diagonal lines from shaft to the LEFT.
  for (let i = 0; i < fullFlags; i++) {
    const baseM = posM;
    posM -= FLAG_OFFSET_M;

    const baseShaft = offsetMetres(input.lat, input.lon, dxUnit * baseM, dyUnit * baseM);
    const tipFlag = offsetMetres(
      input.lat, input.lon,
      // Flag angles slightly back toward the station (downwind end)
      // to mimic the meteorological convention — offset by 0.3 * flag
      // length along the shaft, plus full perpendicular offset.
      dxUnit * (baseM - FULL_FLAG_LEN_M * 0.3) + lxUnit * FULL_FLAG_LEN_M,
      dyUnit * (baseM - FULL_FLAG_LEN_M * 0.3) + lyUnit * FULL_FLAG_LEN_M,
    );
    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [baseShaft, tipFlag] },
      properties: { bucket, kind: "flag" },
    });
  }

  // 4) Half flag — half-length, slightly offset from the shaft tip if
  //    no full flags/pennants preceded it (visual convention).
  if (halfFlags > 0) {
    if (fullFlags === 0 && pennants === 0) {
      posM -= FLAG_OFFSET_M;
    }
    const baseM = posM;
    const baseShaft = offsetMetres(input.lat, input.lon, dxUnit * baseM, dyUnit * baseM);
    const tipFlag = offsetMetres(
      input.lat, input.lon,
      dxUnit * (baseM - HALF_FLAG_LEN_M * 0.3) + lxUnit * HALF_FLAG_LEN_M,
      dyUnit * (baseM - HALF_FLAG_LEN_M * 0.3) + lyUnit * HALF_FLAG_LEN_M,
    );
    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [baseShaft, tipFlag] },
      properties: { bucket, kind: "flag" },
    });
  }

  return features;
}

/**
 * Convert the viewport-sampled wind features (Point + bucket + dir) into
 * a flat array of barb LineString features. Used by WindBarbLayer.
 */
export function buildAllBarbFeatures(
  points: Array<{
    geometry: { coordinates: [number, number] };
    properties: { bucket: number; dir: number };
  }>,
): BarbFeature[] {
  const out: BarbFeature[] = [];
  for (const p of points) {
    const [lon, lat] = p.geometry.coordinates;
    const features = buildBarbFeatures({
      lat,
      lon,
      speedKt: p.properties.bucket,
      dirDeg: p.properties.dir,
    });
    out.push(...features);
  }
  return out;
}

/**
 * Convenience: extract the calm (<5kt) station points so the parent can
 * render them as a CircleLayer (the barb builder above returns no
 * features for calm; we want a visible dot so the user sees that wind
 * data IS present, just light).
 */
export function calmStationFeatures(
  points: Array<{
    geometry: { coordinates: [number, number] };
    properties: { bucket: number; dir: number };
  }>,
) {
  return points
    .filter((p) => p.properties.bucket < 5)
    .map((p) => ({
      type: "Feature" as const,
      geometry: { type: "Point" as const, coordinates: p.geometry.coordinates },
      properties: { bucket: p.properties.bucket },
    }));
}
