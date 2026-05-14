// MapCanvas - the reusable mapbox-gl shell.
//
// Owns:
//   * the DOM container
//   * the Map instance (lifecycle: created on mount, destroyed on unmount)
//   * the style-loaded flag
//   * a context that exposes both to children
//
// Does NOT own:
//   * wind barbs, marks, course line, track, route — those are layers
//   * race-overlay UI, recorder buttons, freshness banners — those are
//     view-level concerns (the live MapView wraps MapCanvas plus its own
//     overlays; the read-only RaceStatsView wraps MapCanvas plus a few
//     layers and that's it)
//
// Children get the map instance via useMapContext() and own their own
// addSource/addLayer setup + cleanup. This keeps each layer component
// small and self-contained, and lets new layers (replay, AIS history,
// fleet positions in D3) slot in without touching the canvas.
//
// Interactivity:
//   * pass `interactive={false}` for read-only views (RaceStatsView).
//     Gestures are still allowed because users want to pan/zoom; this
//     prop instead disables the rotate/pitch controls and the keyboard
//     handlers that don't make sense for a static map.

import { useEffect, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

import { MapContext } from "./MapContext.jsx";

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN;

const DEFAULT_STYLE = "mapbox://styles/mapbox/light-v11";

export function MapCanvas({
  initialCenter = [-87.65, 42.05],
  initialZoom = 8,
  style = DEFAULT_STYLE,
  interactive = true,
  onMapReady = null,
  onViewportChange = null,
  className = "",
  containerStyle = null,
  children,
}) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const [styleLoaded, setStyleLoaded] = useState(false);

  // Stash the callbacks in refs so the boot effect doesn't depend on
  // them — we don't want a parent re-render that changes the function
  // identity to tear down and re-create the map.
  const onMapReadyRef = useRef(onMapReady);
  const onViewportChangeRef = useRef(onViewportChange);
  onMapReadyRef.current = onMapReady;
  onViewportChangeRef.current = onViewportChange;

  useEffect(() => {
    if (mapRef.current || !containerRef.current) return;

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style,
      center: initialCenter,
      zoom: initialZoom,
      // Read-only views still allow pan/zoom — only rotate/pitch are
      // suppressed because they confuse the chart metaphor.
      pitchWithRotate: interactive,
      dragRotate: interactive,
      keyboard: interactive,
    });
    mapRef.current = map;

    const pushViewport = () => {
      if (!onViewportChangeRef.current) return;
      const c = map.getCenter();
      onViewportChangeRef.current({
        zoom: map.getZoom(),
        lat: c.lat,
        lon: c.lng,
      });
    };

    map.on("load", () => {
      setStyleLoaded(true);
      pushViewport();
      onMapReadyRef.current?.(map);
    });
    map.on("moveend", pushViewport);

    return () => {
      map.remove();
      mapRef.current = null;
      setStyleLoaded(false);
    };
    // initialCenter / initialZoom are intentionally not in the dep list
    // — we want them to act as initial values, not props that re-mount
    // the map. Use map.flyTo from the parent for runtime camera moves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      ref={containerRef}
      className={className}
      style={containerStyle || { position: "relative", width: "100%", height: "100%" }}
    >
      <MapContext.Provider value={{ map: mapRef.current, styleLoaded }}>
        {children}
      </MapContext.Provider>
    </div>
  );
}
