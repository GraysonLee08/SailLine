// MapContext - shared handle to the underlying mapbox-gl Map instance so
// composable layer components (MarksLayer, TrackLayer, etc.) can reach
// into the map without prop-drilling through every level.
//
// The context value carries:
//   * map         — the mapbox-gl Map instance, or null before it boots
//   * styleLoaded — true after the "load" event fires; layer effects
//                   should bail out until this flips because addSource /
//                   addLayer require a loaded style
//
// MapCanvas (sibling file) owns the lifecycle and provides the value.
// All layer components are expected to call useMapContext() once at the
// top of their render and gate their effects on styleLoaded.

import { createContext, useContext } from "react";

export const MapContext = createContext({
  map: null,
  styleLoaded: false,
});

export function useMapContext() {
  return useContext(MapContext);
}
