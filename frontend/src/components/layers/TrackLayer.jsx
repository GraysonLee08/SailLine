// TrackLayer - render an array of GPS points as a polyline.
//
// Used by RaceStatsView to show the recorded track on the read-only
// stats map. Could also be reused by a future replay view (D2/D3).
//
// Props:
//   points          — [{lat, lon, ...}] in chronological order
//   color           — stroke color; matches the live MapView's green
//                     by default so the two views look consistent
//   width           — line width in CSS pixels
//   showEndpoints   — when true, drops a small dot at the start
//                     (green) and end (gold) of the track so the
//                     user can orient direction
//
// Note: this layer DOES NOT downsample. The route stats endpoint
// already serves a downsampled speed series for the chart; the
// track polyline gets the raw points so the rendered shape matches
// what was actually recorded. Mapbox handles ~30k vertices fine.

import { useEffect, useRef } from "react";
import mapboxgl from "mapbox-gl";

import { useMapContext } from "../MapContext.jsx";

const SOURCE_ID = "track-layer-source";
const LAYER_ID = "track-layer-line";

function emptyLine() {
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "LineString", coordinates: [] },
  };
}

export function TrackLayer({
  points = [],
  color = "#22a06b",
  width = 3.5,
  showEndpoints = true,
}) {
  const { map, styleLoaded } = useMapContext();
  const endpointMarkersRef = useRef([]);

  useEffect(() => {
    if (!map || !styleLoaded) return;
    if (!map.getSource(SOURCE_ID)) {
      map.addSource(SOURCE_ID, { type: "geojson", data: emptyLine() });
      map.addLayer({
        id: LAYER_ID,
        type: "line",
        source: SOURCE_ID,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": color,
          "line-width": width,
          "line-opacity": 0.92,
        },
      });
    }
    return () => {
      if (map.getLayer && map.getLayer(LAYER_ID)) map.removeLayer(LAYER_ID);
      if (map.getSource && map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
      endpointMarkersRef.current.forEach((m) => m.remove());
      endpointMarkersRef.current = [];
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, styleLoaded]);

  useEffect(() => {
    if (!map || !styleLoaded) return;
    const src = map.getSource(SOURCE_ID);
    if (!src) return;

    // Tear down old endpoint dots before redrawing.
    endpointMarkersRef.current.forEach((m) => m.remove());
    endpointMarkersRef.current = [];

    if (!points.length) {
      src.setData(emptyLine());
      return;
    }

    src.setData({
      type: "Feature",
      properties: {},
      geometry: {
        type: "LineString",
        coordinates: points.map((p) => [p.lon, p.lat]),
      },
    });

    if (showEndpoints && points.length >= 2) {
      const start = points[0];
      const end = points[points.length - 1];
      endpointMarkersRef.current.push(
        new mapboxgl.Marker({
          element: createDot("#22a06b"),
          anchor: "center",
        })
          .setLngLat([start.lon, start.lat])
          .addTo(map),
      );
      endpointMarkersRef.current.push(
        new mapboxgl.Marker({
          element: createDot("#f0b400"),
          anchor: "center",
        })
          .setLngLat([end.lon, end.lat])
          .addTo(map),
      );
    }
  }, [map, styleLoaded, points, showEndpoints]);

  return null;
}

function createDot(color) {
  const el = document.createElement("div");
  el.style.cssText = `
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: ${color};
    border: 2px solid #fff;
    box-shadow: 0 1px 4px rgba(0,0,0,0.30);
  `;
  return el;
}
