// MarksLayer - render a course as a dashed line + numbered markers.
//
// Consumes the map instance via useMapContext(). The line is a
// GeoJSON source named "marks-line" and the numbered markers are
// regular mapboxgl.Marker instances pinned to each lat/lon.
//
// Stateless and read-only: no editing UI, no hover popups, no drag.
// The live editor on RaceEditor.jsx still owns its richer interactive
// rendering; this layer is for read-only views (RaceStatsView, future
// replay).
//
// Props:
//   marks         — [{lat, lon, name?}], 0+ entries; empty is fine.
//   fitOnMount    — when true (default), fits the map to the marks
//                   bbox on first render. Subsequent prop changes
//                   don't re-fit (the user may have panned).
//   color         — line color; defaults to the same blue as the
//                   live MapView's course line for consistency.

import { useEffect, useRef } from "react";
import mapboxgl from "mapbox-gl";

import { useMapContext } from "../MapContext.jsx";

const SOURCE_ID = "marks-line";
const LAYER_ID = "marks-line-layer";

function emptyLine() {
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "LineString", coordinates: [] },
  };
}

export function MarksLayer({
  marks = [],
  fitOnMount = true,
  color = "#1a73e8",
}) {
  const { map, styleLoaded } = useMapContext();
  const markersRef = useRef([]);
  const fittedRef = useRef(false);

  // Add the source + layer once the style is loaded; remove on unmount.
  useEffect(() => {
    if (!map || !styleLoaded) return;
    if (!map.getSource(SOURCE_ID)) {
      map.addSource(SOURCE_ID, { type: "geojson", data: emptyLine() });
      map.addLayer({
        id: LAYER_ID,
        type: "line",
        source: SOURCE_ID,
        paint: {
          "line-color": color,
          "line-width": 3,
          "line-dasharray": [2, 1.5],
        },
      });
    }
    return () => {
      // Tear down on unmount so a re-mount doesn't trip the
      // already-exists branch above.
      if (map.getLayer && map.getLayer(LAYER_ID)) map.removeLayer(LAYER_ID);
      if (map.getSource && map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
      markersRef.current.forEach((m) => m.remove());
      markersRef.current = [];
    };
    // color is intentionally not a dep — to change it, remount the
    // layer with a different key from the parent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, styleLoaded]);

  // Sync marks → markers + source data.
  useEffect(() => {
    if (!map || !styleLoaded) return;
    const src = map.getSource(SOURCE_ID);
    if (!src) return;

    // Replace existing markers each time. Cheap at sailing-race scales
    // (rarely more than ~12 marks).
    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];

    if (!marks.length) {
      src.setData(emptyLine());
      return;
    }

    marks.forEach((mark, i) => {
      const el = createMarkerElement(i + 1, mark.name);
      const marker = new mapboxgl.Marker({ element: el })
        .setLngLat([mark.lon, mark.lat])
        .addTo(map);
      markersRef.current.push(marker);
    });

    src.setData({
      type: "Feature",
      properties: {},
      geometry: {
        type: "LineString",
        coordinates: marks.map((m) => [m.lon, m.lat]),
      },
    });

    if (fitOnMount && !fittedRef.current) {
      fitToMarks(map, marks);
      fittedRef.current = true;
    }
  }, [map, styleLoaded, marks, fitOnMount]);

  return null;
}

function fitToMarks(map, marks) {
  if (!marks?.length) return;
  if (marks.length === 1) {
    map.flyTo({
      center: [marks[0].lon, marks[0].lat],
      zoom: 12,
      duration: 500,
    });
    return;
  }
  const bounds = new mapboxgl.LngLatBounds(
    [marks[0].lon, marks[0].lat],
    [marks[0].lon, marks[0].lat],
  );
  marks.forEach((m) => bounds.extend([m.lon, m.lat]));
  map.fitBounds(bounds, { padding: 80, duration: 500, maxZoom: 13 });
}

function createMarkerElement(label, name) {
  const el = document.createElement("div");
  el.style.cssText = `
    width: 26px;
    height: 26px;
    border-radius: 50%;
    background: #16161a;
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 12px;
    font-weight: 600;
    box-shadow: 0 2px 5px rgba(0,0,0,0.22);
    border: 2px solid #fff;
  `;
  el.textContent = String(label);
  if (name) el.title = name;
  return el;
}
