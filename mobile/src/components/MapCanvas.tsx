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
//   - On first load WITHOUT marks (the user is browsing the race list),
//     try the OS last-known location and center there at zoom 11 so the
//     first-paint barbs reflect "where the user actually is." Falls back
//     to the hardcoded Chicago lakeshore default if no fix is available.
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
  useState,
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
  /**
   * Two-mode toggle, Google-Maps-compass-style:
   *   First call (or after any user-initiated rotation): snap heading to 0
   *     (north up).
   *   Second consecutive call: start following the device's compass heading
   *     so the map rotates as the user turns. Pleasant for racing —
   *     "up" on screen = "ahead of the boat".
   *   Third consecutive call: back to north. And so on.
   */
  toggleCompass: () => void;
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
   * wind-barb features against the new viewport and drive the compass
   * icon's rotation.
   */
  onCameraChanged?: (vp: {
    zoom: number;
    bounds: { south: number; north: number; west: number; east: number };
    centerLat: number;
    centerLon: number;
    headingDeg: number;
  }) => void;
};

const DEFAULT_CENTER: [number, number] = [-87.6, 41.9]; // Chicago lake-shore
const DEFAULT_ZOOM = 11;
// Retry schedule for the cold-start last-known-location lookup. The OS
// usually has a recent fix on hand but Mapbox's locationManager needs a
// beat to surface it — these three attempts cover the ~3s warmup window
// without spamming the API. Cancelled the moment `marks` arrive or the
// camera has otherwise been moved.
const INITIAL_LOCATION_RETRY_MS = [200, 1000, 2500];

const MARK_FEATURE_ID = "marks-src";
const ROUTE_FEATURE_ID = "route-src";

export const MapCanvas = forwardRef<MapCanvasHandle, Props>(function MapCanvas(
  { marks, route, showUser = true, children, onCameraChanged },
  ref,
) {
  const { colors, mode } = useTheme();
  const camRef = useRef<Camera>(null);
  // Follow-user state is driven through the <Camera> prop, not via the
  // imperative setCamera({ followUserLocation }) call which silently
  // no-ops in @rnmapbox/maps v10 when the Camera was mounted without
  // following. Toggling this state is what actually re-binds the
  // camera to the puck position. We set it to true on FAB tap, then
  // immediately back to false so the user can pan/zoom freely
  // afterward without being yanked back to centre.
  const [followingUser, setFollowingUser] = useState(false);

  // Compass toggle mode. "off" = static (north OR a user-set heading);
  // "heading" = camera follows the device compass so up-on-screen is
  // ahead-of-the-boat. The FAB cycles between the two each tap.
  const [compassMode, setCompassMode] = useState<"off" | "heading">("off");

  // Single-shot guard: once we've placed the camera (via the marks-fit
  // effect OR the user-location effect), don't move it again on our own.
  // Imperative actions (locate-me, fitToRace, compass) and user gestures
  // are still free to pan/zoom; this just stops the two mount-time
  // effects from clobbering each other.
  const didInitialCenter = useRef(false);

  // Monochromatic-leaning styles so the overlay data (wind barbs, route
  // polyline, marks) reads clearly without competing with vibrant
  // basemap colours. Light = whites/greys with muted land + water;
  // Dark = near-black with the same muted treatment. Both are far
  // less visually busy than the Outdoors/Streets palette we started
  // with, and the racing-instrument feel of the overlays comes through.
  //
  // For a true single-hue monochrome (no green parks, no blue water),
  // we'd need a custom style published from Mapbox Studio and reference
  // it here by its `mapbox://styles/<account>/<style-id>` URL. Easy
  // next-session change.
  const styleURL = mode === "dark"
    ? Mapbox.StyleURL.Dark
    : Mapbox.StyleURL.Light;

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
  // user-location effect below tries to give a more useful default than
  // the Chicago fallback.
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
    didInitialCenter.current = true;
  }, [initialBounds]);

  // First-paint nudge: if we're mounted without any marks (browsing the
  // race list), bring the camera to the user's last-known location at a
  // useful wind-barb zoom. Mapbox's locationManager often returns null
  // on the very first call (it surfaces the OS cache after the puck
  // mounts), so retry a handful of times over ~3s. Cancelled if marks
  // arrive or another camera action runs in the meantime.
  useEffect(() => {
    if (marks.length > 0 || didInitialCenter.current) return;
    let cancelled = false;
    const timers: ReturnType<typeof setTimeout>[] = [];

    const tryLocate = async () => {
      if (cancelled || didInitialCenter.current) return;
      try {
        const loc = await Mapbox.locationManager.getLastKnownLocation();
        if (cancelled || didInitialCenter.current) return;
        if (loc && typeof loc.coords?.latitude === "number") {
          camRef.current?.setCamera({
            centerCoordinate: [loc.coords.longitude, loc.coords.latitude],
            zoomLevel: DEFAULT_ZOOM,
            animationDuration: 700,
            animationMode: "flyTo",
          });
          didInitialCenter.current = true;
        }
      } catch {
        /* No fix — leave the Chicago default in place. */
      }
    };

    for (const ms of INITIAL_LOCATION_RETRY_MS) {
      timers.push(setTimeout(() => void tryLocate(), ms));
    }

    return () => {
      cancelled = true;
      for (const t of timers) clearTimeout(t);
    };
    // Only run on mount — `marks.length > 0` early return guards the
    // marks-arriving case, and the marks-fit effect above takes over
    // when that happens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useImperativeHandle(
    ref,
    () => ({
      locateMe: async () => {
        // Read the puck's current location and fly there explicitly.
        // Previously we ALSO flipped followUserLocation=true for ~1.5s
        // after the setCamera call, expecting it to catch up to any
        // fresh GPS fix mid-animation. In practice that flip ended the
        // setCamera animation early, which is why "moves more, faster
        // but doesn't get all the way there" was the user-observed
        // behaviour. Drop the follow toggle; if the user has moved by
        // the time the animation completes, they tap locate-me again.
        try {
          const loc = await Mapbox.locationManager.getLastKnownLocation();
          if (loc && typeof loc.coords?.latitude === "number") {
            camRef.current?.setCamera({
              centerCoordinate: [loc.coords.longitude, loc.coords.latitude],
              zoomLevel: 14,
              animationDuration: 900,
              animationMode: "flyTo",
            });
            didInitialCenter.current = true;
            return;
          }
        } catch {
          /* fall through to declarative follow */
        }
        // No last-known fix yet (cold-start location subscription) —
        // declarative follow is the only path that works in that case.
        setFollowingUser(true);
        setTimeout(() => setFollowingUser(false), 2000);
        didInitialCenter.current = true;
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
        didInitialCenter.current = true;
      },
      toggleCompass: () => {
        // Cycle: off → heading → off. When entering "off", snap heading
        // back to 0 (north up).
        //
        // Subtle: the imperative `setCamera({ heading: 0 })` MUST run
        // AFTER React has flushed the compassMode state change to the
        // <Camera> component, otherwise `followUserLocation = true` is
        // still bound and the declarative follow-camera silently
        // clobbers our imperative reset. Deferring with setTimeout(0)
        // pushes the setCamera call to the next event-loop tick, by
        // which time the prop has flipped to false and the imperative
        // call sticks. Tried `useEffect on compassMode` first — it
        // fires too early because Reanimated batches the layout pass
        // ahead of effect cleanup.
        setCompassMode((prev) => {
          const next = prev === "off" ? "heading" : "off";
          if (next === "off") {
            setTimeout(() => {
              camRef.current?.setCamera({
                heading: 0,
                animationDuration: 500,
              });
            }, 50);
          }
          return next;
        });
      },
    }),
    [],
  );

  const handleCameraChanged = (state: {
    properties: {
      zoom: number;
      // Mapbox v11 types `center` and `bounds.{ne,sw}` as Position (number[]),
      // not a strict 2-tuple. Runtime code uses [0]/[1] indexing, which works
      // identically for either width.
      center: number[];
      bounds: { ne: number[]; sw: number[] };
      heading?: number;
    };
  }) => {
    if (!onCameraChanged) return;
    const { zoom, center, bounds, heading } = state.properties;
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
      headingDeg: heading ?? 0,
    });
  };

  return (
    <View style={[styles.root, { backgroundColor: colors.map.background }]}>
      <MapView
        style={styles.map}
        styleURL={styleURL}
        scaleBarEnabled={false}
        // Hide the built-in compass — it's display-only (no tap handler).
        // We render our own rotate-to-north FAB in MapFabs that calls
        // the imperative resetNorth() handle. Same visual position.
        compassEnabled={false}
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
          // Declarative follow — flips true momentarily when the user
          // taps locate-me, anchoring the camera to the location puck.
          // ALSO true when compassMode is "heading" so the camera tracks
          // the device's compass heading (FollowWithHeading mode).
          followUserLocation={followingUser || compassMode === "heading"}
          followUserMode={
            compassMode === "heading"
              ? UserTrackingMode.FollowWithHeading
              : UserTrackingMode.Follow
          }
          followZoomLevel={14}
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
