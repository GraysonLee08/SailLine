// RouteLayer - render a computed isochrone route polyline.
//
// Used by the live MapView (eventually — see the deferred migration
// note in 2026-05-14_post-race-stats-multi-session-plan.md) and any
// future read-only view that wants to overlay a previously-computed
// route on top of the recorded track (replay, what-if D2/D3).
//
// The signature trim-offset animation is preserved: a fresh compute
// reveals the line by tweening line-trim-offset from [0, 1] (fully
// trimmed, invisible) to [0, 0] (no trim, fully visible) over ~700 ms.
// Cache hits skip the animation — the user already saw it last time.
//
// Props:
//   route        — a GeoJSON Feature with a LineString geometry, or
//                  null when there's no route yet.
//   cached       — true when the route came from cache (skip the
//                  reveal animation). Default false.
//   color        — line color; defaults to the same magenta as the
//                  live MapView for consistency.

import { useEffect } from "react";

import { useMapContext } from "../MapContext.jsx";
import { safeAnimate, EASE_OUT_SOFT } from "../../lib/motion";

const SOURCE_ID = "route-layer-source";
const LAYER_ID = "route-layer-line";

function emptyLine() {
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "LineString", coordinates: [] },
  };
}

export function RouteLayer({
  route,
  cached = false,
  color = "#c026d3",
}) {
  const { map, styleLoaded } = useMapContext();

  // Source + layer lifecycle. `lineMetrics: true` is required for
  // line-trim-offset to work.
  useEffect(() => {
    if (!map || !styleLoaded) return;
    if (!map.getSource(SOURCE_ID)) {
      map.addSource(SOURCE_ID, {
        type: "geojson",
        data: emptyLine(),
        lineMetrics: true,
      });
      map.addLayer({
        id: LAYER_ID,
        type: "line",
        source: SOURCE_ID,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": color,
          "line-width": 3,
          "line-opacity": 0.85,
          "line-trim-offset": [0, 1],   // start fully trimmed (invisible)
        },
      });
    }
    return () => {
      if (map.getLayer && map.getLayer(LAYER_ID)) map.removeLayer(LAYER_ID);
      if (map.getSource && map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, styleLoaded]);

  // Update data + animate reveal.
  useEffect(() => {
    if (!map || !styleLoaded) return;
    const src = map.getSource(SOURCE_ID);
    if (!src) return;

    if (!route) {
      src.setData(emptyLine());
      map.setPaintProperty(LAYER_ID, "line-trim-offset", [0, 1]);
      return;
    }

    src.setData(route);

    if (cached) {
      map.setPaintProperty(LAYER_ID, "line-trim-offset", [0, 0]);
      return;
    }

    const trim = { end: 1.0 };
    map.setPaintProperty(LAYER_ID, "line-trim-offset", [0, 1]);
    const ctrl = safeAnimate(trim, {
      end: 0,
      duration: 700,
      easing: EASE_OUT_SOFT,
      onUpdate: () => {
        map.setPaintProperty(LAYER_ID, "line-trim-offset", [0, trim.end]);
      },
    });
    if (!ctrl) {
      // Reduced-motion or hidden tab — snap to revealed.
      map.setPaintProperty(LAYER_ID, "line-trim-offset", [0, 0]);
    }
    return () => {
      if (ctrl?.pause) ctrl.pause();
    };
  }, [map, styleLoaded, route, cached]);

  return null;
}
