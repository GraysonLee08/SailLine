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
// Coordinates are in lat/lon (WGS84). For the small barb size (~few
// dozen metres per flag at typical zoom), we can treat lat/lon as
// locally Euclidean without measurable distortion. Sub-pixel error at
// any practical barb size.
//
// SIZING: barbs are sized in SCREEN PIXELS, then converted to metres
// using the current zoom + latitude. This keeps barbs visually constant
// regardless of zoom level — at z11 a barb is ~14 px shaft, at z14 it
// is still ~14 px (whereas a fixed-metres barb would be 8× larger at
// z14). See `metersPerPixel` for the conversion.

const METRES_PER_DEG_LAT = 111_320; // constant enough

// Target screen sizes in PIXELS. Calibrated so a barb at z11 looks
// roughly identical to the previous fixed-metres barb (~14 px shaft @
// 800 m / 57 m-per-px at lat 42°). Tweak here if barbs ever look too
// small/large at the default racing zoom.
const TARGET_SHAFT_PX = 14;
const TARGET_FULL_FLAG_PX = 6;
const TARGET_HALF_FLAG_PX = 3;
const TARGET_FLAG_OFFSET_PX = 1.5;
const TARGET_PENNANT_LEN_PX = 6;
const TARGET_PENNANT_WIDTH_PX = 3.5;

// Default zoom used when the caller hasn't wired a zoom value through
// (e.g., during unit tests or first render before onCameraChanged
// fires). At z11 the barbs match the pre-pixel-scaling sizes.
const DEFAULT_ZOOM = 11;

/**
 * Mapbox / Web Mercator metres-per-pixel at a given zoom and latitude.
 * Equator base resolution at zoom 0 is 156543.03 m/px; halves each zoom
 * level. cos(lat) shrinks the longitudinal span as latitude rises.
 */
function metersPerPixel(zoom: number, lat: number): number {
  return (156543.03392 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, zoom);
}

/** Resolve per-feature sizes (in metres) for a given station + zoom. */
function sizesFor(zoom: number, lat: number) {
  const mpp = metersPerPixel(zoom, lat);
  return {
    shaftM: TARGET_SHAFT_PX * mpp,
    fullFlagM: TARGET_FULL_FLAG_PX * mpp,
    halfFlagM: TARGET_HALF_FLAG_PX * mpp,
    flagOffsetM: TARGET_FLAG_OFFSET_PX * mpp,
    pennantLenM: TARGET_PENNANT_LEN_PX * mpp,
    pennantWidthM: TARGET_PENNANT_WIDTH_PX * mpp,
  };
}

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
 *
 * @param zoom Current map zoom level. Drives the per-feature metre
 *   sizing so barbs stay screen-constant across zoom. Default = 11.
 */
export function buildBarbFeatures(
  input: BarbInput,
  zoom: number = DEFAULT_ZOOM,
): BarbFeature[] {
  const speedKt = Math.max(0, Math.floor(input.speedKt));
  if (!Number.isFinite(speedKt) || speedKt < 5) return [];

  const { shaftM, fullFlagM, halfFlagM, flagOffsetM, pennantLenM, pennantWidthM } =
    sizesFor(zoom, input.lat);

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
  const shaftTipLon = input.lon + (dxUnit * shaftM) /
    (METRES_PER_DEG_LAT * Math.cos((input.lat * Math.PI) / 180));
  const shaftTipLat = input.lat + (dyUnit * shaftM) / METRES_PER_DEG_LAT;
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
  let posM = shaftM;

  // 2) Pennants — filled triangles. We emit them as closed 4-point
  //    LineStrings; the LineLayer renders them as outlined triangles
  //    which read as filled at our line widths.
  for (let i = 0; i < pennants; i++) {
    const baseM = posM;
    const tipM = posM - pennantLenM;
    posM -= pennantLenM + flagOffsetM;

    const baseShaft = offsetMetres(input.lat, input.lon, dxUnit * baseM, dyUnit * baseM);
    const tipShaft = offsetMetres(input.lat, input.lon, dxUnit * tipM, dyUnit * tipM);
    // Flag flares to the LEFT perpendicular.
    const baseFlag = offsetMetres(
      input.lat, input.lon,
      dxUnit * baseM + lxUnit * pennantWidthM,
      dyUnit * baseM + lyUnit * pennantWidthM,
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
    posM -= flagOffsetM;

    const baseShaft = offsetMetres(input.lat, input.lon, dxUnit * baseM, dyUnit * baseM);
    const tipFlag = offsetMetres(
      input.lat, input.lon,
      // Flag angles slightly back toward the station (downwind end)
      // to mimic the meteorological convention — offset by 0.3 * flag
      // length along the shaft, plus full perpendicular offset.
      dxUnit * (baseM - fullFlagM * 0.3) + lxUnit * fullFlagM,
      dyUnit * (baseM - fullFlagM * 0.3) + lyUnit * fullFlagM,
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
      posM -= flagOffsetM;
    }
    const baseM = posM;
    const baseShaft = offsetMetres(input.lat, input.lon, dxUnit * baseM, dyUnit * baseM);
    const tipFlag = offsetMetres(
      input.lat, input.lon,
      dxUnit * (baseM - halfFlagM * 0.3) + lxUnit * halfFlagM,
      dyUnit * (baseM - halfFlagM * 0.3) + lyUnit * halfFlagM,
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
 *
 * @param zoom Current map zoom level — passed through to each barb so
 *   barbs are sized in screen pixels rather than metres. Default = 11.
 */
export function buildAllBarbFeatures(
  points: Array<{
    geometry: { coordinates: [number, number] };
    properties: { bucket: number; dir: number };
  }>,
  zoom: number = DEFAULT_ZOOM,
): BarbFeature[] {
  const out: BarbFeature[] = [];
  for (const p of points) {
    const [lon, lat] = p.geometry.coordinates;
    const features = buildBarbFeatures(
      {
        lat,
        lon,
        speedKt: p.properties.bucket,
        dirDeg: p.properties.dir,
      },
      zoom,
    );
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
