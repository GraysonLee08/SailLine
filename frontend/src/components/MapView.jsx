// MapView - the always-mounted base layer of the app.
//
// Wind rendering is two-layered:
//
//   1. BASE layer: HRRR @ 0.10 deg over CONUS, or GFS @ 0.25 deg over Hawaii.
//      Always loaded. Covers everywhere a user might pan.
//
//   2. VENUE layer (overlay): HRRR at native ~0.027 deg (~3 km), one per
//      popular sailing area. Loaded only when zoom >= 11 AND the viewport
//      center is inside a venue's bbox. Drawn on top of base.
//
// Where the venue covers, the base layer's barbs are suppressed in that
// bbox so we don't render two densities on top of each other. Outside the
// venue (or below zoom 11), only the base layer renders.
//
// On top of wind, when an active race is set, we render a dashed blue
// course-line through the marks plus numbered read-only markers.
//
// On top of *that*, when the user hits Record, we render a green
// breadcrumb of GPS points captured by useTrackRecorder.
//
// The "better route available" banner is a separate top-of-viewport
// surface fed by the SSE notifications stream.
//
// All overlays use dark frosted-glass surfaces (.glass--dark /
// .glass-card--dark from glass.css). Dark glass on a light map gives
// dramatically more contrast than light-on-light, with the smoky tint
// evoking sunglasses placed on a chart.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

import { useWeather } from "../hooks/useWeather";
import { useGeolocation } from "../hooks/useGeolocation";
import { useCountdown } from "../hooks/useCountdown";
import { useRegion } from "../hooks/useRegion";
import { useTrackRecorder } from "../hooks/useTrackRecorder";
import { useRouting } from "../hooks/useRouting";
import { useRouteNotifications } from "../hooks/useRouteNotifications";
import { useFollowMode } from "../hooks/useFollowMode";
import { useAutoStartRecorder } from "../hooks/useAutoStartRecorder";
import { useAutoStopRecorder } from "../hooks/useAutoStopRecorder";
import { useRouteFreshnessCheck } from "../hooks/useRouteFreshnessCheck";
import { useHeelGauge } from "../hooks/useHeelGauge";
import { ComputeRouteButton, RouteStatus } from "./RouteControls.jsx";
import { BetterRouteBanner } from "./BetterRouteBanner.jsx";
import { PermissionBanner } from "./PermissionBanner.jsx";
import { usePermissionStatus } from "../hooks/usePermissionStatus";
import { AnimatedDigit, splitSecondsFromCountdown } from "./AnimatedDigit.jsx";
import { regionCenter, venueForPoint, VENUE_ZOOM_THRESHOLD } from "../lib/regions";
import { generateBarbImages, computeFeatures } from "../lib/windBarb";
import { formatLat, formatLon } from "../lib/latlon";
import { safeAnimate, EASE_OUT_SOFT } from "../lib/motion";
import { DEFAULT_PHONE_AXIS, PHONE_AXES } from "../lib/imuAxes";

const PHONE_AXIS_STORAGE_KEY = "sailline.phoneAxis";

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN;

const REGION_FLY_ZOOM = 7;
const GPS_FLY_ZOOM = 13;
const COURSE_FIT_PADDING = 140;
const COURSE_FIT_MAX_ZOOM = 12;

const RACE_OVERLAY_TOP_DEFAULT = 12;
const RACE_OVERLAY_TOP_WITH_BANNER = 76;

const EMPTY_FC = { type: "FeatureCollection", features: [] };

export function MapView({
  activeRace,
  onEditActive,
  onClearActive,
  onRaceCompleted = null,
}) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const courseMarkersRef = useRef([]);
  const fittedRaceIdRef = useRef(null);
  const gpsHandledRef = useRef(false);
  const flownRegionRef = useRef(null);
  const lastEaseAtRef = useRef(0);
  const [styleLoaded, setStyleLoaded] = useState(false);

  const [viewport, setViewport] = useState(null);

  const base = useRegion(activeRace);
  const baseSource = base.defaultSource;

  const venue = useMemo(() => {
    if (!viewport) return null;
    if (viewport.zoom < VENUE_ZOOM_THRESHOLD) return null;
    return venueForPoint(viewport.lat, viewport.lon);
  }, [viewport]);

  const { data: baseWeather, validTime, ageMinutes } = useWeather(
    base.name,
    baseSource,
  );
  const { data: venueWeather } = useWeather(venue?.name ?? null, "hrrr");

  const { position } = useGeolocation();

  // Phone axis — fore-aft (long edge along centerline) vs port-stbd.
  // Persisted globally on the device because the placement of the
  // phone in the cockpit is typically a per-boat habit, not per-race.
  // Surfaced as a toggle in the race overlay.
  const [phoneAxis, setPhoneAxis] = useState(() => {
    try {
      const stored = localStorage.getItem(PHONE_AXIS_STORAGE_KEY);
      return PHONE_AXES.includes(stored) ? stored : DEFAULT_PHONE_AXIS;
    } catch {
      return DEFAULT_PHONE_AXIS;
    }
  });
  const onPhoneAxisChange = useCallback((next) => {
    if (!PHONE_AXES.includes(next)) return;
    setPhoneAxis(next);
    try {
      localStorage.setItem(PHONE_AXIS_STORAGE_KEY, next);
    } catch {
      /* best effort */
    }
  }, []);

  const recorder = useTrackRecorder(activeRace?.id ?? null, { phoneAxis });
  const routing = useRouting(activeRace?.id ?? null);
  const notif = useRouteNotifications(activeRace?.id ?? null);
  const followMode = useFollowMode(activeRace?.id ?? null);
  const permissionStatus = usePermissionStatus();

  // Active calibration for the LIVE gauge — separate from
  // `recorder.pendingCalibration` (which clears on flush ack). Persisted
  // per-race in localStorage so a tab reload doesn't dump the zero.
  // Server-side calibration is the authoritative one for post-race
  // stats; this lives purely to keep the on-screen readout sensible
  // after the user taps Zero at the dock.
  const calibrationStorageKey = activeRace?.id
    ? `sailline.activeCalibration.${activeRace.id}`
    : null;
  const [activeCalibration, setActiveCalibration] = useState(() => {
    if (!calibrationStorageKey) return null;
    try {
      const raw = localStorage.getItem(calibrationStorageKey);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  });
  // Re-load when the active race changes so we don't bleed one race's
  // zero into another.
  useEffect(() => {
    if (!calibrationStorageKey) {
      setActiveCalibration(null);
      return;
    }
    try {
      const raw = localStorage.getItem(calibrationStorageKey);
      setActiveCalibration(raw ? JSON.parse(raw) : null);
    } catch {
      setActiveCalibration(null);
    }
  }, [calibrationStorageKey]);

  // Live heel/pitch readout. Sampler only attaches when we have an
  // active race (the only screen the gauge appears on).
  const heelGauge = useHeelGauge({
    enabled: !!activeRace,
    phoneAxis,
    calibration: activeCalibration,
  });

  const onCaptureCalibration = useCallback(() => {
    const captured = recorder.captureCalibration();
    if (!captured) return;
    setActiveCalibration({
      heel_zero_offset_deg: captured.heel_zero_offset_deg,
      pitch_zero_offset_deg: captured.pitch_zero_offset_deg,
      captured_at: captured.captured_at,
    });
    if (calibrationStorageKey) {
      try {
        localStorage.setItem(calibrationStorageKey, JSON.stringify(captured));
      } catch {
        /* best effort */
      }
    }
  }, [recorder, calibrationStorageKey]);

  // Auto-arm the recorder for T-5. `auto_start_enabled` defaults to true
  // for races created before 0007 too — the migration sets the column
  // default and the API serialises true on read.
  const autoStart = useAutoStartRecorder({
    raceId: activeRace?.id ?? null,
    startAtIso: activeRace?.start_at ?? null,
    enabled: activeRace?.auto_start_enabled !== false,
    recording: recorder.recording,
    start: recorder.start,
  });

  // Auto-stop once the boat finishes the course (last + second-to-last
  // marks rounded, then 5min buffer). Re-uses the same `auto_start_enabled`
  // flag for now — Session B's column gates the whole auto-record cycle;
  // splitting it into a separate `auto_stop_enabled` is a future tweak if
  // anyone wants to opt into one without the other.
  const autoStop = useAutoStopRecorder({
    raceId: activeRace?.id ?? null,
    marks: activeRace?.marks ?? [],
    points: recorder.points,
    recording: recorder.recording,
    enabled: activeRace?.auto_start_enabled !== false,
    stop: recorder.stop,
    // Surface the auto-stop event so AppView can navigate to the
    // stats view as soon as recording cuts off. The Cloud Run Job
    // is already in flight by this point (final-mark trigger in
    // tracks.py); the stats endpoint surfaces partial data while
    // the AI summary populates.
    onFired: onRaceCompleted,
  });

  // T-5 wind-drift check against the computed route's start-mark wind.
  // Active any time a route exists — but the banner is only shown inside
  // the pre-start window (see RaceOverlay below) so it doesn't haunt the
  // user post-gun.
  const startMark = useMemo(() => {
    const m = activeRace?.marks?.[0];
    if (!m || !Number.isFinite(m.lat) || !Number.isFinite(m.lon)) return null;
    return { lat: m.lat, lon: m.lon };
  }, [activeRace]);
  const freshness = useRouteFreshnessCheck({
    routeMeta: routing.meta,
    baseWeather,
    startMark,
  });

  const followModeRef = useRef(followMode);
  followModeRef.current = followMode;

  useEffect(() => {
    if (mapRef.current || !containerRef.current) return;

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: "mapbox://styles/mapbox/light-v11",
      center: regionCenter(base),
      zoom: REGION_FLY_ZOOM,
    });
    mapRef.current = map;
    flownRegionRef.current = base.name;

    const pushViewport = () => {
      const c = map.getCenter();
      setViewport({ zoom: map.getZoom(), lat: c.lat, lon: c.lng });
    };

    map.on("load", () => {
      const images = generateBarbImages();
      Object.entries(images).forEach(([id, dataUrl]) => {
        const img = new Image(64, 64);
        img.onload = () => {
          if (!map.hasImage(id)) map.addImage(id, img);
        };
        img.src = dataUrl;
      });

      map.addSource("wind-base", { type: "geojson", data: EMPTY_FC });
      map.addLayer({
        id: "wind-barbs-base",
        type: "symbol",
        source: "wind-base",
        layout: {
          "icon-image": ["concat", "barb-", ["get", "bucket"]],
          "icon-rotate": ["get", "dir"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": 0.8,
        },
      });

      map.addSource("wind-venue", { type: "geojson", data: EMPTY_FC });
      map.addLayer({
        id: "wind-barbs-venue",
        type: "symbol",
        source: "wind-venue",
        layout: {
          "icon-image": ["concat", "barb-", ["get", "bucket"]],
          "icon-rotate": ["get", "dir"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": 0.8,
        },
      });

      map.addSource("course", { type: "geojson", data: emptyLine() });
      map.addLayer({
        id: "course-line",
        type: "line",
        source: "course",
        paint: {
          "line-color": "#1a73e8",
          "line-width": 3,
          "line-dasharray": [2, 1.5],
        },
      });

      map.addSource("track", { type: "geojson", data: emptyLine() });
      map.addLayer({
        id: "track-line",
        type: "line",
        source: "track",
        paint: {
          "line-color": "#22a06b",
          "line-width": 3.5,
          "line-opacity": 0.92,
        },
      });

      map.addSource("route", {
        type: "geojson",
        data: emptyLine(),
        lineMetrics: true, // required for line-trim-offset
      });
      map.addLayer({
        id: "route-line",
        type: "line",
        source: "route",
        layout: {
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#c026d3",
          "line-width": 3,
          "line-opacity": 0.85,
          "line-trim-offset": [0, 1], // start fully trimmed (invisible)
        },
      });

      pushViewport();
      map.on("moveend", pushViewport);

      // User-gesture detection: flip follow-mode off when the user manually
      // pans/zooms/rotates. Programmatic camera moves (map.easeTo) don't
      // have an `originalEvent`, so we filter to user-initiated only.
      const userGestureHandler = (e) => {
        if (e.originalEvent && followModeRef.current.following) {
          followModeRef.current.setFollowing(false);
        }
      };
      map.on("dragstart", userGestureHandler);
      map.on("zoomstart", userGestureHandler);
      map.on("rotatestart", userGestureHandler);

      setStyleLoaded(true);
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!styleLoaded || !baseWeather) return;
    const map = mapRef.current;
    const src = map.getSource("wind-base");
    if (!src) return;

    const features = computeFeatures(map, baseWeather, venue?.bbox ?? null);
    src.setData({ type: "FeatureCollection", features });
  }, [baseWeather, viewport, venue, styleLoaded]);

  useEffect(() => {
    if (!styleLoaded) return;
    const map = mapRef.current;
    const src = map.getSource("wind-venue");
    if (!src) return;

    if (!venueWeather) {
      src.setData(EMPTY_FC);
      return;
    }
    const features = computeFeatures(map, venueWeather);
    src.setData({ type: "FeatureCollection", features });
  }, [venueWeather, viewport, styleLoaded]);

  useEffect(() => {
    if (!mapRef.current) return;
    if (flownRegionRef.current === base.name) return;
    flownRegionRef.current = base.name;

    if (activeRace) return;
    if (position) return;

    mapRef.current.flyTo({
      center: regionCenter(base),
      zoom: REGION_FLY_ZOOM,
      duration: 1200,
    });
  }, [base, activeRace, position]);

  useEffect(() => {
    if (!mapRef.current || !position) return;
    if (gpsHandledRef.current) return;
    gpsHandledRef.current = true;
    if (activeRace) return;
    mapRef.current.flyTo({
      center: [position.lon, position.lat],
      zoom: GPS_FLY_ZOOM,
      duration: 1500,
    });
  }, [position, activeRace]);

  const syncKey = useMemo(() => {
    if (!activeRace) return "";
    return (
      activeRace.id +
      "::" +
      (activeRace.marks || [])
        .map((m) => `${m.name}|${m.lat}|${m.lon}`)
        .join("~")
    );
  }, [activeRace]);

  useEffect(() => {
    if (!styleLoaded) return;
    const map = mapRef.current;
    if (!map) return;

    courseMarkersRef.current.forEach((m) => m.remove());
    courseMarkersRef.current = [];

    const src = map.getSource("course");
    if (!src) return;

    if (!activeRace || !activeRace.marks?.length) {
      src.setData(emptyLine());
      fittedRaceIdRef.current = null;
      return;
    }

    activeRace.marks.forEach((mark, i) => {
      const el = createMarkerElement(i + 1);
      const popup = new mapboxgl.Popup({
        offset: 18,
        closeButton: false,
        closeOnClick: false,
      }).setHTML(buildPopupHTML(mark));

      el.addEventListener("mouseenter", () => {
        popup.setLngLat([mark.lon, mark.lat]).addTo(map);
      });
      el.addEventListener("mouseleave", () => popup.remove());

      const marker = new mapboxgl.Marker({ element: el })
        .setLngLat([mark.lon, mark.lat])
        .addTo(map);

      courseMarkersRef.current.push(marker);
    });

    src.setData({
      type: "Feature",
      properties: {},
      geometry: {
        type: "LineString",
        coordinates: activeRace.marks.map((m) => [m.lon, m.lat]),
      },
    });

    if (fittedRaceIdRef.current !== activeRace.id) {
      fitToMarks(map, activeRace.marks);
      fittedRaceIdRef.current = activeRace.id;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncKey, styleLoaded]);

  useEffect(() => {
    if (!styleLoaded) return;
    const map = mapRef.current;
    if (!map) return;
    const src = map.getSource("track");
    if (!src) return;

    if (recorder.points.length === 0) {
      src.setData(emptyLine());
      return;
    }
    src.setData({
      type: "Feature",
      properties: {},
      geometry: {
        type: "LineString",
        coordinates: recorder.points.map((p) => [p.lon, p.lat]),
      },
    });
  }, [recorder.points, styleLoaded]);

  useEffect(() => {
    if (!styleLoaded) return;
    const map = mapRef.current;
    if (!map) return;
    const src = map.getSource("route");
    if (!src) return;

    if (!routing.route) {
      src.setData(emptyLine());
      map.setPaintProperty("route-line", "line-trim-offset", [0, 1]);
      return;
    }

    // Set the data first so the line geometry is in place.
    src.setData(routing.route);

    // Skip the draw-on for cache hits — server signals via meta.cached.
    // The user already saw this route compute on a prior session; no need
    // to replay the reveal.
    if (routing.meta?.cached) {
      map.setPaintProperty("route-line", "line-trim-offset", [0, 0]);
      return;
    }

    // Animate line-trim-offset from [0, 1] (invisible) to [0, 0] (full).
    // anime.js v4 tweens a plain object's property; setPaintProperty fires
    // each frame to push the new trim end through to Mapbox.
    const trim = { end: 1.0 };
    map.setPaintProperty("route-line", "line-trim-offset", [0, 1]);

    const ctrl = safeAnimate(trim, {
      end: 0,
      duration: 700,
      easing: EASE_OUT_SOFT,
      onUpdate: () => {
        map.setPaintProperty("route-line", "line-trim-offset", [0, trim.end]);
      },
    });

    // safeAnimate returns null under reduced-motion / hidden — snap.
    if (!ctrl) {
      map.setPaintProperty("route-line", "line-trim-offset", [0, 0]);
    }

    return () => {
      if (ctrl?.pause) ctrl.pause();
    };
  }, [routing.route, routing.meta?.cached, styleLoaded]);

  // One easeTo per second max. Geolocation can fire at high rates and
  // accuracy bounce would jitter the camera if every fix triggered a
  // pan. The throttle is intentional — most sailing race speeds mean
  // the user moves <100m/s, so 1Hz updates feel live without churn.
  useEffect(() => {
    if (!styleLoaded) return;
    if (!followMode.following) return;
    if (!position?.lat || !position?.lon) return;
    const map = mapRef.current;
    if (!map) return;

    const now = Date.now();
    if (now - lastEaseAtRef.current < 1000) return;
    lastEaseAtRef.current = now;

    map.easeTo({
      center: [position.lon, position.lat],
      duration: 600,
      easing: (t) => 1 - Math.pow(1 - t, 3),  // ease-out cubic
    });
  }, [position, followMode.following, styleLoaded]);

  // Recenter pill button bumps recenterTick. We want a stronger camera
  // move than the steady-state follow (zoom in if currently far out).
  useEffect(() => {
    if (!styleLoaded) return;
    if (followMode.recenterTick === 0) return;  // ignore initial value
    if (!position?.lat || !position?.lon) return;
    const map = mapRef.current;
    if (!map) return;
    map.easeTo({
      center: [position.lon, position.lat],
      zoom: Math.max(map.getZoom(), 14),
      duration: 800,
      easing: (t) => 1 - Math.pow(1 - t, 3),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [followMode.recenterTick, styleLoaded]);

  const raceOverlayTop = notif.alternative
    ? RACE_OVERLAY_TOP_WITH_BANNER
    : RACE_OVERLAY_TOP_DEFAULT;

  return (
    <div style={styles.shell}>
      <div ref={containerRef} style={styles.map} />

      <BetterRouteBanner
        alternative={notif.alternative}
        onAccept={() =>
          notif.accept((feature) => routing.applyAlternative(feature))
        }
        onDismiss={notif.dismiss}
      />

      <PermissionBanner
        status={permissionStatus}
        recording={recorder.recording}
      />

      {activeRace && (
        <RaceOverlay
          race={activeRace}
          recording={recorder.recording}
          queueLength={recorder.queueLength}
          recorderError={recorder.error}
          onToggleRecord={() =>
            recorder.recording ? recorder.stop() : recorder.start()
          }
          onEdit={onEditActive}
          onClear={onClearActive}
          routing={routing}
          topOffset={raceOverlayTop}
          autoStart={autoStart}
          autoStop={autoStop}
          freshness={freshness}
          onRecompute={routing.compute}
          phoneAxis={phoneAxis}
          onPhoneAxisChange={onPhoneAxisChange}
          onCaptureCalibration={onCaptureCalibration}
          activeCalibration={activeCalibration}
          heelReading={heelGauge.reading}
          orientationSupported={heelGauge.supported}
          orientationPermission={recorder.orientationPermission}
        />
      )}

      {/* Wind status (top-left) - dark frosted glass. */}
      {baseWeather && (
        <div className="glass--dark" style={styles.windOverlay}>
          <span style={styles.label}>{baseSource.toUpperCase()}</span>
          <span style={styles.value}>
            valid {validTime?.toISOString().slice(11, 16)}Z
          </span>
          <span style={styles.age}>{ageMinutes}m old</span>
        </div>
      )}

      {!followMode.following && position?.lat && (
        <button
          type="button"
          onClick={followMode.recenter}
          className="glass-button--dark"
          aria-label="Re-center on my position"
          style={styles.recenterPill}
        >
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none"
               stroke="currentColor" strokeWidth="2.2"
               strokeLinecap="round" strokeLinejoin="round"
               style={{ verticalAlign: "-3px", marginRight: 6 }}>
            <circle cx="12" cy="12" r="3" />
            <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
          </svg>
          Re-center
        </button>
      )}
    </div>
  );
}

function RaceOverlay({
  race,
  recording,
  queueLength,
  recorderError,
  onToggleRecord,
  onEdit,
  onClear,
  routing,
  topOffset,
  autoStart,
  autoStop,
  freshness,
  onRecompute,
  phoneAxis,
  onPhoneAxisChange,
  onCaptureCalibration,
  activeCalibration,
  heelReading,
  orientationSupported,
  orientationPermission,
}) {
  const cd = useCountdown(race.start_at);

  // Show the "armed" hint and the freshness banner only inside the
  // pre-start window. After the gun, the banner becomes noise: the user
  // has already crossed and either accepted the route or didn't. The
  // phone-placement tip is shown alongside the armed badge so the user
  // sees the reminder right before recording kicks in.
  const inPreStartWindow =
    !cd.isUnset && !cd.isPast && (cd.msUntil ?? 0) <= 5 * 60 * 1000;
  const showArmed = autoStart?.armed && !recording && inPreStartWindow;
  const showFreshness =
    freshness?.ready && freshness.stale && !cd.isPast && !cd.isUnset;

  // "Pre-race" for the Zero button — calibration is dock-only. Visible
  // any time before the gun (covers the case where the user shows up
  // earlier than T-5 and wants to zero straight away). Once cd.isPast
  // flips, the button hides — re-zeroing mid-race is intentionally not
  // wired in this version. We also hide it on races without a start
  // time (cd.isUnset) so we don't show a dead control.
  const showZeroButton = !cd.isUnset && !cd.isPast && orientationSupported;
  const denied = orientationPermission === "denied";

  return (
    <div
      className="glass-card--dark"
      style={{ ...styles.raceOverlay, top: topOffset }}
    >
      <div style={styles.raceMain}>
        <div style={styles.raceLabel}>Active race</div>
        <div style={styles.raceName}>{race.name}</div>
        <div
          style={{
            ...styles.raceCountdown,
            // On dark glass: light gray for unset/past, brighter blue for live.
            color: cd.isUnset
              ? "var(--paper-ink-3)"
              : cd.isPast
                ? "var(--paper-ink-3)"
                : "#7eb6ff",
          }}
        >
          {cd.isUnset
            ? "No start time set"
            : cd.isPast
              ? cd.label
              : (() => {
                  const { prefix, seconds } = splitSecondsFromCountdown(cd.label);
                  if (!prefix) return `Starts in ${cd.label}`;
                  return (
                    <>
                      Starts in {prefix}
                      <AnimatedDigit value={seconds} />
                    </>
                  );
                })()}
        </div>
        {showArmed && (
          <div style={styles.armedHint}>
            ● Auto-recording armed · mount your phone in a fixed location
          </div>
        )}
        {showFreshness && (
          <div style={styles.freshnessBanner}>
            <div style={styles.freshnessText}>
              Wind shifted since route was computed
              {" · "}
              Δ{Math.round(freshness.deltaDirDeg)}°,{" "}
              {freshness.deltaSpeedKt.toFixed(1)} kt
            </div>
            <button
              onClick={onRecompute}
              disabled={routing?.loading}
              style={styles.freshnessBtn}
              aria-label="Recompute route with current wind"
            >
              {routing?.loading ? "Recomputing…" : "Recompute"}
            </button>
          </div>
        )}
        {recording && autoStop?.armed && (
          <div style={styles.armedHint}>
            ● Course complete · auto-stop in {formatMmSs(autoStop.msUntilStop)}
          </div>
        )}
        {recording && queueLength > 0 && (
          <div style={styles.queueHint}>
            {queueLength} pt{queueLength === 1 ? "" : "s"} pending
            {recorderError ? " · offline" : ""}
          </div>
        )}
        {!recording && recorderError && (
          <div style={styles.recordError}>{recorderError}</div>
        )}

        {/* Phone-axis toggle + Zero calibration. Visible whenever an
            active race is set; the Zero button hides once the gun goes
            off. */}
        <div style={styles.calibrationRow}>
          <span style={styles.calibrationLabel}>Phone:</span>
          <button
            type="button"
            onClick={() => onPhoneAxisChange("fore-aft")}
            style={{
              ...styles.axisPill,
              ...(phoneAxis === "fore-aft" ? styles.axisPillOn : {}),
            }}
            aria-pressed={phoneAxis === "fore-aft"}
            title="Phone long edge along boat centerline"
          >
            Fore-aft
          </button>
          <button
            type="button"
            onClick={() => onPhoneAxisChange("port-stbd")}
            style={{
              ...styles.axisPill,
              ...(phoneAxis === "port-stbd" ? styles.axisPillOn : {}),
            }}
            aria-pressed={phoneAxis === "port-stbd"}
            title="Phone long edge across the boat"
          >
            Port-stbd
          </button>
          {showZeroButton && (
            <button
              type="button"
              onClick={onCaptureCalibration}
              style={styles.zeroBtn}
              aria-label="Zero heel and pitch"
              title="Capture current orientation as zero heel/pitch. Do this at the dock with the boat level."
            >
              Zero
            </button>
          )}
        </div>
        {activeCalibration && (
          <div style={styles.calibrationStatus}>
            ✓ Zeroed (heel {activeCalibration.heel_zero_offset_deg.toFixed(1)}°,
            {" "}pitch {activeCalibration.pitch_zero_offset_deg.toFixed(1)}°)
          </div>
        )}
        {denied && (
          <div style={styles.queueHint}>
            Heel/pitch unavailable — orientation permission denied. Tap Stop
            then Start again to retry.
          </div>
        )}

        {/* Live heel/pitch readout while recording. Subtle — sailors
            don't want to stare at it, but a glance is useful. */}
        {recording && heelReading && (
          <div style={styles.heelReadout}>
            <span style={styles.heelReadoutLabel}>Heel</span>
            <span style={styles.heelReadoutValue}>
              {heelReading.heelDeg.toFixed(0)}°
            </span>
            <span style={styles.heelReadoutLabel}>Pitch</span>
            <span style={styles.heelReadoutValue}>
              {heelReading.pitchDeg.toFixed(0)}°
            </span>
          </div>
        )}

        {routing && (
          <RouteStatus meta={routing.meta} error={routing.error} />
        )}
      </div>
      <div style={styles.raceActions}>
        {routing && (
          <ComputeRouteButton
            loading={routing.loading}
            hasRoute={!!routing.route}
            onClick={routing.compute}
          />
        )}
        <button
          onClick={onToggleRecord}
          className={recording ? "" : "glass-button--dark"}
          style={recording ? styles.recordBtnOn : styles.recordBtn}
          aria-label={recording ? "Stop recording" : "Start recording"}
          aria-pressed={recording}
          title={recording ? "Stop recording" : "Start recording"}
        >
          <span
            style={recording ? styles.recordDotOn : styles.recordDot}
            aria-hidden
          />
          <span style={styles.recordLabel}>
            {recording ? "Rec" : "Record"}
          </span>
        </button>
        <button
          onClick={onEdit}
          className="glass-button--dark"
          style={styles.raceEditBtn}
        >
          Edit
        </button>
        <button
          onClick={onClear}
          className="glass-button--dark"
          style={styles.raceClearBtn}
          aria-label="Clear active race"
          title="Clear active race"
        >
          ✕
        </button>
      </div>
    </div>
  );
}

// -- Helpers ---------------------------------------------------------

/** "M:SS" countdown for the auto-stop badge. Null/<=0 → "0:00". */
function formatMmSs(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "0:00";
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function emptyLine() {
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "LineString", coordinates: [] },
  };
}

function fitToMarks(map, marks) {
  if (!marks?.length) return;
  if (marks.length === 1) {
    map.flyTo({ center: [marks[0].lon, marks[0].lat], zoom: 12, duration: 800 });
    return;
  }
  const bounds = new mapboxgl.LngLatBounds(
    [marks[0].lon, marks[0].lat],
    [marks[0].lon, marks[0].lat],
  );
  marks.forEach((m) => bounds.extend([m.lon, m.lat]));
  map.fitBounds(bounds, {
    padding: COURSE_FIT_PADDING,
    duration: 800,
    maxZoom: COURSE_FIT_MAX_ZOOM,
  });
}

function createMarkerElement(label) {
  const el = document.createElement("div");
  el.style.cssText = `
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: #16161a;
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,0.25);
    border: 2px solid #fff;
  `;
  el.textContent = String(label);
  return el;
}

function buildPopupHTML(mark) {
  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  const latLine = `${esc(formatLat(mark.lat))} &nbsp;·&nbsp; ${esc(formatLon(mark.lon))}`;
  const desc = mark.description
    ? `<div style="margin-top:6px;color:#5a5a64;font-size:12px;line-height:1.4;">${esc(mark.description)}</div>`
    : "";
  return `
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-width:180px;">
      <div style="font-weight:600;font-size:14px;color:#16161a;">${esc(mark.name)}</div>
      <div style="margin-top:4px;color:#5a5a64;font-size:12px;font-variant-numeric:tabular-nums;">${latLine}</div>
      ${desc}
    </div>
  `;
}

const styles = {
  shell: { position: "relative", width: "100%", height: "100vh" },
  map: { position: "absolute", inset: 0 },

  // Wind status (top-left). All text colors are inverse - light on
  // the dark glass surface. The .glass--dark class supplies bg / blur
  // / border / shadow / radius / base color.
  windOverlay: {
    position: "absolute",
    top: 12,
    left: 12,
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "8px 14px",
    fontFamily: "var(--mono)",
    fontSize: 12,
    zIndex: 5,
  },
  label: {
    fontWeight: 600,
    letterSpacing: "0.05em",
    color: "var(--paper-ink)",
  },
  value: { color: "var(--paper-ink-2)" },
  age: { color: "var(--paper-ink-3)" },

  // Race overlay (top-center). Dark glass card; all text inverse.
  raceOverlay: {
    position: "absolute",
    left: "50%",
    transform: "translateX(-50%)",
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "10px 14px 10px 18px",
    minWidth: 240,
    maxWidth: "calc(100vw - 140px)",
    zIndex: 5,
    transition: "top 0.32s cubic-bezier(0.2, 0.9, 0.3, 1.15)",
  },
  raceMain: {
    display: "flex",
    flexDirection: "column",
    gap: 2,
    minWidth: 0,
    flex: 1,
  },
  raceLabel: {
    fontSize: 10,
    color: "var(--paper-ink-3)",
    textTransform: "uppercase",
    letterSpacing: "0.08em",
    fontWeight: 500,
  },
  raceName: {
    fontSize: 15,
    color: "var(--paper-ink)",
    fontWeight: 500,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  raceCountdown: {
    fontFamily: "var(--mono)",
    fontSize: 13,
    fontVariantNumeric: "tabular-nums",
    fontWeight: 500,
  },
  queueHint: {
    fontSize: 11,
    color: "var(--paper-ink-3)",
    fontFamily: "var(--mono)",
    marginTop: 2,
  },
  // Subtle blue-tinted line. Appears inside the T-5 window once the
  // auto-start timer is scheduled; goes away the moment the recorder
  // actually flips to recording.
  armedHint: {
    fontSize: 11,
    color: "#7eb6ff",
    marginTop: 4,
    letterSpacing: "0.01em",
  },
  // Amber-ish callout above the route status — distinct from
  // recorderError red so it doesn't read as a fault.
  freshnessBanner: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    marginTop: 6,
    padding: "6px 10px",
    border: "1px solid rgba(255, 196, 102, 0.55)",
    background: "rgba(255, 196, 102, 0.15)",
    borderRadius: "var(--r-sm)",
    maxWidth: 320,
  },
  freshnessText: {
    flex: 1,
    fontSize: 11,
    color: "#ffd089",
    lineHeight: 1.3,
    fontVariantNumeric: "tabular-nums",
  },
  freshnessBtn: {
    flexShrink: 0,
    padding: "4px 10px",
    border: "1px solid rgba(255, 196, 102, 0.6)",
    background: "rgba(255, 196, 102, 0.18)",
    borderRadius: "var(--r-sm)",
    color: "#ffd089",
    fontSize: 11,
    fontWeight: 500,
    cursor: "pointer",
    fontFamily: "inherit",
  },
  recordError: {
    // Brighter red for legibility on dark.
    fontSize: 11,
    color: "#ff8a92",
    marginTop: 2,
    maxWidth: 220,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },

  // Phone-axis toggle + Zero button. Compact row beneath the countdown
  // / armed hint. Dark-glass pill styling that matches the language but
  // is small enough to disappear unless the user looks at it.
  calibrationRow: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    marginTop: 6,
    flexWrap: "wrap",
  },
  calibrationLabel: {
    fontSize: 10,
    color: "var(--paper-ink-3)",
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    marginRight: 2,
  },
  axisPill: {
    padding: "3px 9px",
    fontSize: 11,
    border: "1px solid rgba(255,255,255,0.18)",
    background: "rgba(255,255,255,0.04)",
    color: "var(--paper-ink-2)",
    borderRadius: 999,
    cursor: "pointer",
    fontFamily: "inherit",
    letterSpacing: "0.01em",
  },
  axisPillOn: {
    background: "rgba(126,182,255,0.16)",
    color: "#cfe0ff",
    border: "1px solid rgba(126,182,255,0.5)",
  },
  zeroBtn: {
    padding: "3px 11px",
    marginLeft: 4,
    fontSize: 11,
    border: "1px solid rgba(126,182,255,0.5)",
    background: "rgba(126,182,255,0.12)",
    color: "#cfe0ff",
    borderRadius: 999,
    cursor: "pointer",
    fontFamily: "inherit",
    fontWeight: 500,
    letterSpacing: "0.02em",
  },
  calibrationStatus: {
    fontSize: 10,
    color: "#9fd29f",
    marginTop: 2,
    fontVariantNumeric: "tabular-nums",
    fontFamily: "var(--mono)",
  },

  // Live heel/pitch readout. Mono digits so the values don't jitter
  // visually as they oscillate.
  heelReadout: {
    display: "flex",
    alignItems: "baseline",
    gap: 6,
    marginTop: 6,
    fontFamily: "var(--mono)",
    fontVariantNumeric: "tabular-nums",
  },
  heelReadoutLabel: {
    fontSize: 10,
    color: "var(--paper-ink-3)",
    textTransform: "uppercase",
    letterSpacing: "0.06em",
  },
  heelReadoutValue: {
    fontSize: 14,
    color: "var(--paper-ink)",
    fontWeight: 500,
    minWidth: 36,
    textAlign: "right",
  },
  raceActions: {
    display: "flex",
    gap: 6,
    flexShrink: 0,
    alignItems: "center",
  },

  // Buttons inherit visuals from the .glass-button--dark class. These
  // inline styles only handle dimensions, padding, and typography.
  recordBtn: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    height: 44,
    minWidth: 80,
    padding: "0 14px",
    fontSize: 13,
    cursor: "pointer",
    fontFamily: "inherit",
    fontWeight: 500,
  },
  // Recording-active deliberately breaks the glass language. Soft red
  // glow + brighter red border read as alarming against dark glass.
  recordBtnOn: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    height: 44,
    minWidth: 80,
    padding: "0 14px",
    border: "1px solid rgba(255, 107, 122, 0.55)",
    background: "rgba(176, 0, 32, 0.32)",
    backdropFilter: "blur(16px) saturate(180%)",
    WebkitBackdropFilter: "blur(16px) saturate(180%)",
    borderRadius: "var(--r-md)",
    fontSize: 13,
    color: "#ffb3ba",
    cursor: "pointer",
    fontFamily: "inherit",
    fontWeight: 600,
    boxShadow: "0 1px 0 rgba(255, 255, 255, 0.10) inset",
    transition: "background 0.15s, border-color 0.15s, transform 0.08s",
  },
  recordDot: {
    display: "inline-block",
    width: 10,
    height: 10,
    borderRadius: "50%",
    border: "2px solid #ff8a92",
    background: "transparent",
  },
  recordDotOn: {
    display: "inline-block",
    width: 10,
    height: 10,
    borderRadius: "50%",
    background: "#ff5b6e",
    boxShadow: "0 0 0 2px rgba(255, 91, 110, 0.28)",
  },
  recordLabel: { fontVariantNumeric: "tabular-nums" },
  raceEditBtn: {
    padding: "8px 14px",
    fontSize: 13,
    cursor: "pointer",
    fontFamily: "inherit",
    fontWeight: 500,
  },
  raceClearBtn: {
    width: 34,
    height: 34,
    fontSize: 14,
    cursor: "pointer",
    padding: 0,
    fontFamily: "inherit",
  },
  recenterPill: {
    position: "fixed",
    bottom: 24,
    right: 24,
    zIndex: 1000,
    padding: "10px 14px",
    fontSize: 13,
    fontWeight: 500,
    cursor: "pointer",
    fontFamily: "inherit",
  },
};
