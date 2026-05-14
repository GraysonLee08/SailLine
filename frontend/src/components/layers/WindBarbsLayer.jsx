// WindBarbsLayer - render a wind field as adaptive-density barbs.
//
// Two-tier rendering is supported via the `excludeBbox` prop, mirroring
// the base / venue split in the live MapView: a base instance renders
// over the full map; a venue instance renders only inside the venue's
// bbox and the base instance excludes that area to avoid double-density.
//
// Props:
//   weather       — { lats, lons, u, v } as returned by useWeather().
//                   Pass null/undefined when no data is loaded; the
//                   layer renders an empty feature collection.
//   excludeBbox   — { minLat, maxLat, minLon, maxLon } | null. Cells
//                   inside this box are skipped (used by the base
//                   instance to hand off to a venue overlay).
//   sourceId      — unique source id; required so multiple instances
//                   on the same map (base + venue) don't collide.
//   targetSpacing — desired pixel spacing between barbs at the current
//                   zoom. Default 70 — looks right on retina+desktop.
//
// The barb-image cache (one PNG per 5-kt speed bucket) is shared
// across instances of this layer through map.hasImage(). The first
// instance to mount calls generateBarbImages() and addImage() for each
// bucket; subsequent instances notice the images are already on the
// map and skip the call.

import { useEffect } from "react";

import { useMapContext } from "../MapContext.jsx";
import { bilerpUV, generateBarbImages, uvToSpeedDir } from "../../lib/windBarb";

const EMPTY_FC = { type: "FeatureCollection", features: [] };

export function WindBarbsLayer({
  weather,
  excludeBbox = null,
  sourceId = "wind-barbs-source",
  layerId = null,
  targetSpacing = 70,
}) {
  const { map, styleLoaded } = useMapContext();
  const effectiveLayerId = layerId || `${sourceId}-symbols`;

  // First mount: ensure the barb sprite sheet is added to the map.
  // hasImage() lets us share images across multiple WindBarbsLayer
  // instances (base + venue) without re-uploading.
  useEffect(() => {
    if (!map || !styleLoaded) return;
    const images = generateBarbImages();
    Object.entries(images).forEach(([id, dataUrl]) => {
      if (map.hasImage(id)) return;
      const img = new Image(64, 64);
      img.onload = () => {
        if (!map.hasImage(id)) map.addImage(id, img);
      };
      img.src = dataUrl;
    });
  }, [map, styleLoaded]);

  // Source + symbol layer lifecycle.
  useEffect(() => {
    if (!map || !styleLoaded) return;
    if (!map.getSource(sourceId)) {
      map.addSource(sourceId, { type: "geojson", data: EMPTY_FC });
      map.addLayer({
        id: effectiveLayerId,
        type: "symbol",
        source: sourceId,
        layout: {
          "icon-image": ["concat", "barb-", ["get", "bucket"]],
          "icon-rotate": ["get", "dir"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": 0.8,
        },
      });
    }
    return () => {
      if (map.getLayer && map.getLayer(effectiveLayerId)) {
        map.removeLayer(effectiveLayerId);
      }
      if (map.getSource && map.getSource(sourceId)) {
        map.removeSource(sourceId);
      }
    };
  }, [map, styleLoaded, sourceId, effectiveLayerId]);

  // Recompute features on weather change AND on viewport change (the
  // adaptive-density algorithm reads the current zoom and bounds).
  useEffect(() => {
    if (!map || !styleLoaded) return;
    const src = map.getSource(sourceId);
    if (!src) return;
    if (!weather) {
      src.setData(EMPTY_FC);
      return;
    }
    const handler = () => {
      const features = computeFeatures(map, weather, excludeBbox, targetSpacing);
      src.setData({ type: "FeatureCollection", features });
    };
    handler();
    map.on("moveend", handler);
    return () => {
      map.off("moveend", handler);
    };
  }, [map, styleLoaded, weather, excludeBbox, targetSpacing, sourceId]);

  return null;
}

function computeFeatures(map, weather, excludeBbox, targetSpacingPx) {
  const { lats, lons, u, v } = weather;
  const zoom = map.getZoom();
  const bounds = map.getBounds();
  const centerLat = map.getCenter().lat;

  const pxPerDeg =
    (256 * Math.pow(2, zoom) * Math.cos((centerLat * Math.PI) / 180)) / 360;
  const targetDeg = targetSpacingPx / pxPerDeg;

  const nativeLatStep = Math.abs(lats[1] - lats[0]);
  const nativeLonStep = Math.abs(lons[1] - lons[0]);
  const nativeStep = Math.max(nativeLatStep, nativeLonStep);

  const south = bounds.getSouth();
  const north = bounds.getNorth();
  const west = bounds.getWest();
  const east = bounds.getEast();

  const inExcluded = (lat, lon) =>
    excludeBbox &&
    lat >= excludeBbox.minLat &&
    lat <= excludeBbox.maxLat &&
    lon >= excludeBbox.minLon &&
    lon <= excludeBbox.maxLon;

  const features = [];

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

function makeFeature(lon, lat, u, v) {
  const { speedKt, dirDeg } = uvToSpeedDir(u, v);
  const bucket = Math.min(Math.round(speedKt / 5) * 5, 65);
  return {
    type: "Feature",
    geometry: { type: "Point", coordinates: [lon, lat] },
    properties: { bucket, dir: dirDeg },
  };
}
