import { useEffect, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

import { useWeather } from "../hooks/useWeather";
import { useGeolocation } from "../hooks/useGeolocation";
import { uvToSpeedDir, bilerpUV, generateBarbImages } from "../lib/windBarb";

// TODO(v1.x): wind particle / flow visualization. Two paths investigated and
// shelved for v1:
//   1. Custom WebGL layer adapted from mapbox/webgl-wind. Geometry verified
//      correct (constant-velocity test passed) but real wind data produced
//      false vortices we never fully isolated. See git history for the
//      windParticleLayer.js attempt.
//   2. Mapbox's official raster-particle. Visually beautiful, fully
//      maintained, but cost is prohibitive: ~$5-6k/month at hourly HRRR
//      publish frequency for one region.
// Barbs are accurate and good enough for v1.

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN;

// Target on-screen spacing between barbs in CSS pixels. Drives both the
// stride when zoomed out (decimates the native HRRR grid) and the
// synthetic spacing when zoomed in (interpolates between native points).
//
// HRRR is regridded to ~0.1° (~11km at our latitudes), so a typical 4nm
// race course at zoom 13 contains 0–1 native points. Below the native
// resolution we synthesize intermediate barbs via bilinear interpolation
// in windBarb.bilerpUV — visually smoother but adding no information
// beyond ~11km. Honest meteorological caveat: the interpolated values
// are a presentation layer, not new data.
const TARGET_BARB_SPACING_PX = 70;

// Lake Michigan fallback when GPS is denied / inland / unavailable.
const DEFAULT_CENTER = [-87.0, 43.5];
const DEFAULT_ZOOM = 13;

/**
 * Compute the wind barb features to render at the current map view.
 *
 * Adaptive density: aims for ~constant on-screen barb spacing
 * (TARGET_BARB_SPACING_PX) regardless of zoom level.
 */
function computeFeatures(map, weather) {
  const { lats, lons, u, v } = weather;
  const zoom = map.getZoom();
  const bounds = map.getBounds();
  const centerLat = map.getCenter().lat;

  // Web Mercator: 256 px per tile, 2^zoom tiles per world width, scaled
  // by cos(lat) to convert longitude degrees to ground distance.
  const pxPerDeg =
    (256 * Math.pow(2, zoom) * Math.cos((centerLat * Math.PI) / 180)) / 360;
  const targetDeg = TARGET_BARB_SPACING_PX / pxPerDeg;

  const nativeLatStep = Math.abs(lats[1] - lats[0]);
  const nativeLonStep = Math.abs(lons[1] - lons[0]);
  const nativeStep = Math.max(nativeLatStep, nativeLonStep);

  const south = bounds.getSouth();
  const north = bounds.getNorth();
  const west = bounds.getWest();
  const east = bounds.getEast();

  const features = [];

  if (targetDeg >= nativeStep) {
    // Zoomed out: native grid is denser than we want. Stride through it
    // and clip to the visible viewport so we don't ship offscreen barbs
    // to Mapbox.
    const stride = Math.max(1, Math.round(targetDeg / nativeStep));
    for (let i = 0; i < lats.length; i += stride) {
      const lat = lats[i];
      if (lat < south || lat > north) continue;
      for (let j = 0; j < lons.length; j += stride) {
        const lon = lons[j];
        if (lon < west || lon > east) continue;
        features.push(makeFeature(lon, lat, u[i][j], v[i][j]));
      }
    }
  } else {
    // Zoomed in: native grid is too sparse. Walk a synthetic grid at
    // targetDeg spacing and bilerp u/v at each point.
    //
    // Snap the start lat/lon to a multiple of targetDeg so the grid
    // doesn't shift while panning — keeps barb positions stable to the
    // eye instead of crawling.
    const startLat = Math.ceil(south / targetDeg) * targetDeg;
    const startLon = Math.ceil(west / targetDeg) * targetDeg;

    for (let lat = startLat; lat <= north; lat += targetDeg) {
      for (let lon = startLon; lon <= east; lon += targetDeg) {
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

export function MapView() {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const [styleLoaded, setStyleLoaded] = useState(false);

  const { data: weather, validTime, ageMinutes } = useWeather("great_lakes", "hrrr");
  const { position } = useGeolocation();

  // Initialize map once. Assigning mapRef.current synchronously (before
  // .on("load")) makes the strict-mode double-mount in dev bail on the
  // second pass, which would otherwise cancel the style request.
  useEffect(() => {
    if (mapRef.current || !containerRef.current) return;

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: "mapbox://styles/mapbox/light-v11",
      center: DEFAULT_CENTER,
      zoom: DEFAULT_ZOOM,
    });
    mapRef.current = map;

    map.on("load", () => {
      const images = generateBarbImages();
      Object.entries(images).forEach(([id, dataUrl]) => {
        const img = new Image(64, 64);
        img.onload = () => {
          if (!map.hasImage(id)) map.addImage(id, img);
        };
        img.src = dataUrl;
      });

      map.addSource("wind", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });

      map.addLayer({
        id: "wind-barbs",
        type: "symbol",
        source: "wind",
        layout: {
          "icon-image": ["concat", "barb-", ["get", "bucket"]],
          "icon-rotate": ["get", "dir"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": 0.8,
        },
      });

      setStyleLoaded(true);
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // Recompute and push wind features whenever:
  //   - a new weather payload lands
  //   - the user pans or zooms (moveend covers both, fires once after
  //     the gesture settles, so no extra debouncing needed)
  useEffect(() => {
    if (!styleLoaded || !weather) return;
    const map = mapRef.current;
    const source = map.getSource("wind");
    if (!source) return;

    const update = () => {
      const features = computeFeatures(map, weather);
      source.setData({ type: "FeatureCollection", features });
    };

    update();
    map.on("moveend", update);
    return () => {
      map.off("moveend", update);
    };
  }, [weather, styleLoaded]);

  // Recenter on browser GPS when it resolves. User can pan freely after.
  useEffect(() => {
    if (!mapRef.current || !position) return;
    mapRef.current.flyTo({
      center: [position.lon, position.lat],
      zoom: DEFAULT_ZOOM,
      duration: 1500,
    });
  }, [position]);

  return (
    <div style={styles.shell}>
      <div ref={containerRef} style={styles.map} />
      {weather && (
        <div style={styles.overlay}>
          <span style={styles.label}>HRRR</span>
          <span style={styles.value}>
            valid {validTime?.toISOString().slice(11, 16)}Z
          </span>
          <span style={styles.age}>{ageMinutes}m old</span>
        </div>
      )}
    </div>
  );
}

const styles = {
  shell: { position: "relative", width: "100%", height: "100vh" },
  map: { position: "absolute", inset: 0 },
  overlay: {
    position: "absolute",
    top: 12,
    left: 12,
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "8px 14px",
    background: "rgba(255, 255, 255, 0.94)",
    backdropFilter: "blur(8px)",
    borderRadius: 8,
    boxShadow: "0 1px 3px rgba(0, 0, 0, 0.08)",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 12,
    color: "#1f2937",
  },
  label: { fontWeight: 600, letterSpacing: "0.05em" },
  value: { color: "#475569" },
  age: { color: "#94a3b8" },
};
