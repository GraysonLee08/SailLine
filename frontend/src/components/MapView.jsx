import { useEffect, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

import { useWeather } from "../hooks/useWeather";
import { useGeolocation } from "../hooks/useGeolocation";
import { uvToSpeedDir, generateBarbImages } from "../lib/windBarb";

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

// Render every Nth grid point in each axis. HRRR is ~100×191 (~19k points);
// SUBSAMPLE=4 gives a readable, dense field at the default zoom.
const SUBSAMPLE = 1;

// Lake Michigan fallback when GPS is denied / inland / unavailable.
const DEFAULT_CENTER = [-87.0, 43.5];
const DEFAULT_ZOOM = 13;

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

  // Push wind data into the source whenever a new payload lands.
  useEffect(() => {
    if (!styleLoaded || !weather) return;
    const map = mapRef.current;
    const source = map.getSource("wind");
    if (!source) return;

    const { lats, lons, u, v } = weather;
    const features = [];
    for (let i = 0; i < lats.length; i += SUBSAMPLE) {
      for (let j = 0; j < lons.length; j += SUBSAMPLE) {
        const { speedKt, dirDeg } = uvToSpeedDir(u[i][j], v[i][j]);
        const bucket = Math.min(Math.round(speedKt / 5) * 5, 65);
        features.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: [lons[j], lats[i]] },
          properties: { bucket, dir: dirDeg },
        });
      }
    }
    source.setData({ type: "FeatureCollection", features });
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
