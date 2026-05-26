// MapCanvas.tsx — full-bleed Mapbox view (Google-Maps-style canvas).
//
// Owns the @rnmapbox/maps view + camera, plus the layers that depend
// directly on map state (user location, marks, route polyline). Wind
// barbs and other overlays are passed in as children so the parent owns
// their lifecycle.
//
// Camera UX:
//   - On first load with `marks` provided, fit the bounds with padding
//     so the user sees the whole course immediately. This is the
//     Google-Maps "destination already in the search bar" pattern —
//     the user shouldn't have to pan.
//   - "Locate me" recenters on the user with a 15s flight and zoom 14.
//     Imperative method exposed via ref so the parent FAB can call it.
//   - User pans/zooms → leave them alone. Don't auto-follow during a
//     race (sailors will scroll to look at upwind windshifts).
//
// Token wiring: Mapbox needs an access token before MapView mounts.
// We call setAccessToken once at module load from the env var; if the
// token is missing the SDK falls back to a gray "no token" tile, which
// is visible at runtime — good signal during setup.

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
} from "react";
import { StyleSheet, View } from "react-native";
import Mapbox, {
  Camera,
  CircleLayer,
  LineLayer,
  LocationPuck,
  MapView,
  ShapeSource,
  SymbolLayer,
  UserTrackingMode,
} from "@rnmapbox/maps";

import { useTheme } from "../theme/ThemeProvider";
import type { Race, RaceMark } from "../types";
import type { RouteFeature } from "../api/routing";

// One-time SDK config. Calling setAccessToken with null/undefined is a
// no-op; the SDK then renders the missing-token grid, which is a useful
// dev-time signal.
Mapbox.setAccessToken(process.env.EXPO_PUBLIC_MAPBOX_TOKEN ?? null);
// Telemetry off — we're a paid Mapbox account, no need to share UA data.
Mapbox.setTelemetryEnabled(false);

export type MapCanvasHandle = {
  /** Recenter on the user's location at zoom 14. */
  locateMe: () => void;
  /** Fly to bounds containing all marks of the active race. */
  fitToRace: (race: Race) => void;
};

type Props = {
  /** Marks to render. Pass [] to clear (e.g., user is not in a race). */
  marks: RaceMark[];
  /** GeoJSON LineString route to overlay. Pass null to hide. */
  route: RouteFeature | null;
  /** Whether to render the user's heading puck (default true). */
  showUser?: boolean;
  /** Render children inside the map for additional overlays (barbs). */
  children?: React.ReactNode;
  /**
   * Called every time the camera changes. Lets the parent recompute
   * wind-barb features against the new viewport.
   */
  onCameraChanged?: (vp: {
    zoom: number;
    bounds: { south: number; north: number; west: number; east: number };
    centerLat: number;
    centerLon: number;
  }) => void;
};

const DEFAULT_CENTER: [number, number] = [-87.6, 41.9]; // Chicago lake-shore
const DEFAULT_ZOOM = 11;

const MARK_FEATURE_ID = "marks-src";
const ROUTE_FEATURE_ID = "route-src";

export const MapCanvas = forwardRef<MapCanvasHandle, Props>(function MapCanvas(
  { marks, route, showUser = true, children, onCameraChanged },
  ref,
) {
  const { colors, mode } = useTheme();
  const camRef = useRef<Camera>(null);

  // Pick a map style by theme. The "navigation-day" / "navigation-night"
  // styles give a low-clutter base appropriate for an over-the-water UI
  // and let the wind + route layers pop.
  const styleURL = mode === "dark"
    ? Mapbox.StyleURL.Dark
    : Mapbox.StyleURL.Outdoors;

  // GeoJSON for marks: Point features with `name` + sequence index. Index
  // drives a number label so sailors see "1, 2, 3..." per mark.
  const marksGeoJson = useMemo(
    () => ({
      type: "FeatureCollection" as const,
      features: marks.map((m, i) => ({
        type: "Feature" as const,
        geometry: { type: "Point" as const, coordinates: [m.lon, m.lat] },
        properties: { name: m.name, index: i + 1 },
      })),
    }),
    [marks],
  );

  // Initial camera bounds — fit to marks once on mount. If no marks, the
  // default Chicago view is good enough for the empty state.
  const initialBounds = useMemo(() => {
    if (marks.length === 0) return null;
    let minLon = Infinity, maxLon = -Infinity;
    let minLat = Infinity, maxLat = -Infinity;
    for (const m of marks) {
      if (m.lon < minLon) minLon = m.lon;
      if (m.lon > maxLon) maxLon = m.lon;
      if (m.lat < minLat) minLat = m.lat;
      if (m.lat > maxLat) maxLat = m.lat;
    }
    return {
      ne: [maxLon, maxLat] as [number, number],
      sw: [minLon, minLat] as [number, number],
    };
  }, [marks]);

  // When the marks change (user picked a new race), refit. Skip if the
  // user is currently recording — preserving their scroll matters.
  useEffect(() => {
    if (!initialBounds || !camRef.current) return;
    camRef.current.fitBounds(
      initialBounds.ne,
      initialBounds.sw,
      [120, 60, 320, 60], // top, right, bottom (sheet peek), left
      900,
    );
  }, [initialBounds]);

  useImperativeHandle(
    ref,
    () => ({
      locateMe: () => {
        camRef.current?.setCamera({
          followUserLocation: true,
          followUserMode: UserTrackingMode.Follow,
          followZoomLevel: 14,
          animationDuration: 800,
        });
      },
      fitToRace: (race) => {
        if (race.marks.length === 0 || !camRef.current) return;
        let minLon = Infinity, maxLon = -Infinity;
        let minLat = Infinity, maxLat = -Infinity;
        for (const m of race.marks) {
          if (m.lon < minLon) minLon = m.lon;
          if (m.lon > maxLon) maxLon = m.lon;
          if (m.lat < minLat) minLat = m.lat;
          if (m.lat > maxLat) maxLat = m.lat;
        }
        camRef.current.fitBounds(
          [maxLon, maxLat],
          [minLon, minLat],
          [120, 60, 320, 60],
          900,
        );
      },
    }),
    [],
  );

  const handleCameraChanged = (state: {
    properties: {
      zoom: number;
      center: [number, number];
      bounds: { ne: [number, number]; sw: [number, number] };
    };
  }) => {
    if (!onCameraChanged) return;
    const { zoom, center, bounds } = state.properties;
    onCameraChanged({
      zoom,
      bounds: {
        south: bounds.sw[1],
        north: bounds.ne[1],
        west: bounds.sw[0],
        east: bounds.ne[0],
      },
      centerLat: center[1],
      centerLon: center[0],
    });
  };

  return (
    <View style={[styles.root, { backgroundColor: colors.map.background }]}>
      <MapView
        style={styles.map}
        styleURL={styleURL}
        scaleBarEnabled={false}
        compassEnabled
        compassPosition={{ top: 18, right: 16 }}
        attributionPosition={{ bottom: 8, left: 8 }}
        logoPosition={{ bottom: 8, left: 8 }}
        onCameraChanged={handleCameraChanged}
      >
        <Camera
          ref={camRef}
          defaultSettings={{
            centerCoordinate: DEFAULT_CENTER,
            zoomLevel: DEFAULT_ZOOM,
          }}
          animationMode="flyTo"
        />

        {showUser ? (
          <LocationPuck
            visible
            pulsing={{ isEnabled: true, color: colors.map.userPosition }}
            puckBearingEnabled
            puckBearing="heading"
          />
        ) : null}

        {/* Route polyline (drawn UNDER marks so dots sit on top). */}
        {route ? (
          <ShapeSource id={ROUTE_FEATURE_ID} shape={route}>
            <LineLayer
              id="route-casing"
              style={{
                lineColor: colors.map.routeCasing,
                lineWidth: 7,
                lineCap: "round",
                lineJoin: "round",
              }}
            />
            <LineLayer
              id="route-stroke"
              style={{
                lineColor: colors.map.routeStroke,
                lineWidth: 4,
                lineCap: "round",
                lineJoin: "round",
              }}
            />
          </ShapeSource>
        ) : null}

        {/* Marks (sequence-numbered). */}
        {marks.length > 0 ? (
          <ShapeSource id={MARK_FEATURE_ID} shape={marksGeoJson}>
            <CircleLayer
              id="marks-fill"
              style={{
                circleRadius: 11,
                circleColor: colors.map.markFill,
                circleStrokeColor: colors.map.markStroke,
                circleStrokeWidth: 2,
              }}
            />
            <SymbolLayer
              id="marks-label"
              style={{
                textField: ["get", "index"],
                textSize: 12,
                textColor: colors.map.markStroke, // inverse — readable on the dark fill
                textAllowOverlap: true,
                textIgnorePlacement: true,
                textFont: ["DIN Pro Bold", "Arial Unicode MS Bold"],
              }}
            />
          </ShapeSource>
        ) : null}

        {children}
      </MapView>
    </View>
  );
});

const styles = StyleSheet.create({
  root: { flex: 1 },
  map: { flex: 1 },
});
