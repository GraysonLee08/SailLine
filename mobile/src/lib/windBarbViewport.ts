// lib/windBarbViewport.ts — viewport-agnostic wind barb feature builder.
//
// @sailline/shared's `computeFeatures` takes a mapbox-gl `Map` instance
// for getZoom/getBounds/getCenter. RN uses @rnmapbox/maps whose camera
// state is exposed differently (RegionPayload from MapView's
// onCameraChanged event). This module re-implements the same logic in
// terms of a plain viewport object so we don't introduce a fake "map"
// shim or duplicate the algorithm.
//
// The algorithm itself is identical to the web's — sample native grid on
// a stride when zoomed out, bilinear interpolate when zoomed in.

import { bilerpUV, makeFeature } from "@sailline/shared";

type WindGrid = {
  lats: number[];
  lons: number[];
  u: number[][];
  v: number[][];
};

export type Viewport = {
  /** Mapbox-style zoom level (continuous, not integer). */
  zoom: number;
  /** Visible bounds in lat/lon. */
  bounds: {
    south: number;
    north: number;
    west: number;
    east: number;
  };
  /** Center latitude — used to convert px-spacing to degrees. */
  centerLat: number;
};

export type BBox = {
  minLat: number;
  maxLat: number;
  minLon: number;
  maxLon: number;
};

const TARGET_BARB_SPACING_PX = 70;

export function computeBarbFeatures(
  viewport: Viewport,
  weather: WindGrid,
  excludeBbox: BBox | null = null,
) {
  const { lats, lons, u, v } = weather;
  const { zoom, bounds, centerLat } = viewport;

  const pxPerDeg =
    (256 * Math.pow(2, zoom) * Math.cos((centerLat * Math.PI) / 180)) / 360;
  const targetDeg = TARGET_BARB_SPACING_PX / pxPerDeg;

  const nativeLatStep = Math.abs(lats[1] - lats[0]);
  const nativeLonStep = Math.abs(lons[1] - lons[0]);
  const nativeStep = Math.max(nativeLatStep, nativeLonStep);

  const { south, north, west, east } = bounds;

  const inExcluded = (lat: number, lon: number) =>
    excludeBbox != null &&
    lat >= excludeBbox.minLat &&
    lat <= excludeBbox.maxLat &&
    lon >= excludeBbox.minLon &&
    lon <= excludeBbox.maxLon;

  const features: ReturnType<typeof makeFeature>[] = [];

  if (targetDeg >= nativeStep) {
    const stride = Math.max(1, Math.round(targetDeg / nativeStep));
    for (let i = 0; i < lats.length; i += stride) {
      const lat = lats[i];
      if (lat < south || lat > north) continue;
      for (let j = 0; j < lons.length; j += stride) {
        const lon = lons[j];
        if (lon < west || lon > east) continue;
        if (inExcluded(lat, lon)) continue;
        features.push(makeFeature(lon, lat, u[i][j], v[i][j]));
      }
    }
  } else {
    const startLat = Math.ceil(south / targetDeg) * targetDeg;
    const startLon = Math.ceil(west / targetDeg) * targetDeg;
    for (let lat = startLat; lat <= north; lat += targetDeg) {
      for (let lon = startLon; lon <= east; lon += targetDeg) {
        if (inExcluded(lat, lon)) continue;
        const sample = bilerpUV(weather, lat, lon);
        if (sample) features.push(makeFeature(lon, lat, sample.u, sample.v));
      }
    }
  }
  return features;
}
