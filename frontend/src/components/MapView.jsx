// MapView — the always-mounted base layer of the app.
//
// Wind rendering is two-layered:
//
//   1. BASE layer: HRRR @ 0.10° over CONUS, or GFS @ 0.25° over Hawaii.
//      Always loaded. Covers everywhere a user might pan.
//
//   2. VENUE layer (overlay): HRRR at native ~0.027° (~3 km), one per
//      popular sailing area. Loaded only when zoom ≥ 11 AND the viewport
//      center is inside a venue's bbox. Drawn on top of base.
//
// Where the venue covers, the base layer's barbs are suppressed in that
// bbox so we don't render two densities on top of each other. Outside the
// venue (or below zoom 11), only the base layer renders.
//
// Region-base auto-detection is in `useRegion`. Venue selection is here
// because it depends on viewport state (which only the map knows).
//
// On top of wind, when an active race is set, we render a dashed blue
// course-line through the marks plus numbered read-only markers.
// Drag/edit happens in RaceEditor.
//
// On top of *that*, when the user hits Record, we render a green
// breadcrumb of GPS points captured by `useTrackRecorder`. The recorder
// owns its own buffer + offline queue; this view just consumes its
// `points` array and pushes them to a Mapbox GeoJSON source.

import { useEffect, useMemo, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

import { useWeather } from "../hooks/useWeather";
import { useGeolocation } from "../hooks/useGeolocation";
import { useCountdown } from "../hooks/useCountdown";
import { useRegion } from "../hooks/useRegion";
import { useTrackRecorder } from "../hooks/useTrackRecorder";
import { useRouting } from "../hooks/useRouting";
import { ComputeRouteButton, RouteStatus } from "./RouteControls.jsx";
import { regionCenter, venueForPoint, VENUE_ZOOM_THRESHOLD } from "../lib/regions";
import { uvToSpeedDir, bilerpUV, generateBarbImages } from "../lib/windBarb";
import { formatLat, formatLon } from "../lib/latlon";

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
// stride when zoomed out (decimates the native grid) and the synthetic
// spacing when zoomed in (interpolates between native points).
const TARGET_BARB_SPACING_PX = 70;

// Initial map zoom — the map mounts at the resolved base region's center.
const REGION_FLY_ZOOM = 7;
const GPS_FLY_ZOOM = 13;

// Padding for fitBounds when an active race is loaded. Generous (140px) so
// there's room around the course and the top-center race overlay never
// covers a mark. maxZoom: 12 keeps tight courses from zooming in absurdly
// close so the user sees the whole route in context.
const COURSE_FIT_PADDING = 140;
const COURSE_FIT_MAX_ZOOM = 12;

/**
 * Compute the wind barb features to render at the current map view.
 *
 * Adaptive density: aims for ~constant on-screen barb spacing
 * (TARGET_BARB_SPACING_PX) regardless of zoom level.
 *
 * @param {Map} map        Mapbox map instance
 * @param {object} weather Wind grid payload with lats/lons/u/v
 * @param {{minLat,maxLat,minLon,maxLon}|null} excludeBbox
 *   When set, points falling inside this bbox are skipped. Used by the
 *   base layer to avoid drawing barbs where the venue overlay covers.
 */
function computeFeatures(map, weather, excludeBbox = null) {
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

  const inExcluded = (lat, lon) =>
    excludeBbox &&
    lat >= excludeBbox.minLat &&
    lat <= excludeBbox.maxLat &&
    lon >= excludeBbox.minLon &&
    lon <= excludeBbox.maxLon;

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
        if (inExcluded(lat, lon)) continue;
        features.push(makeFeature(lon, lat, u[i][j], v[i][j]));
      }
    }
  } else {
    // Zoomed in: native grid is too sparse. Walk a synthetic grid at
    // targetDeg spacing and bilerp u/v at each point. Snap the start
    // lat/lon to a multiple of targetDeg so the grid doesn't shift while
    // panning — keeps barb positions stable to the eye.
    const startLat = Math.ceil(south / targetDeg) * targetDeg;
    const startLon = Math.ceil(west / targetDeg) * targetDeg;

    for (let lat = startLat; lat <= north; lat += targetDeg) {
      for (let lon = startLon; lon <= east; lon += targetDeg) {
        if (inExcluded(lat, lon)) continue;
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

const EMPTY_FC = { type: "FeatureCollection", features: [] };

export function MapView({ activeRace, onEditActive, onClearActive }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const courseMarkersRef = useRef([]);
  const fittedRaceIdRef = useRef(null);
  const gpsHandledRef = useRef(false);
  const flownRegionRef = useRef(null);
  const [styleLoaded, setStyleLoaded] = useState(false);

  // Viewport state — used to decide whether to load a venue overlay.
  // Updated on `moveend` (covers pan + zoom, fires once per gesture).
  const [viewport, setViewport] = useState(null); // { zoom, lat, lon }

  // Resolve base region from user identity / race override.
  const base = useRegion(activeRace);
  const baseSource = base.defaultSource;

  // Resolve venue from current viewport — only when zoomed in past the
  // threshold AND the viewport center sits inside a venue's bbox.
  const venue = useMemo(() => {
    if (!viewport) return null;
    if (viewport.zoom < VENUE_ZOOM_THRESHOLD) return null;
    return venueForPoint(viewport.lat, viewport.lon);
  }, [viewport]);

  const { data: baseWeather, validTime, ageMinutes } = useWeather(
    base.name,
    baseSource,
  );
  // Pass `null` when no venue → useWeather skips fetching.
  const { data: venueWeather } = useWeather(venue?.name ?? null, "hrrr");

  const { position } = useGeolocation();

  // Track recorder. Disabled when there's no active race — start() will
  // surface an error if the user somehow hits the button without one.
  const recorder = useTrackRecorder(activeRace?.id ?? null);

  // Routing — POST /api/routing/compute against the current HRRR wind.
  // Disabled when no active race; UI gates the button.
  const routing = useRouting(activeRace?.id ?? null);

  // Initialize map once. Center on the resolved base so the first paint
  // is already in the right place — useRegion returns synchronously from
  // localStorage or DEFAULT_BASE_REGION.
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

    // Seed viewport state on load and update on moveend.
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

      // Base wind layer — drawn first.
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

      // Venue overlay — drawn on top of base.
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

      // Course-line on top of wind — visually consistent with RaceEditor
      // (blue dashed). Last-added wins for layer order on a Mapbox style,
      // so the course always draws over wind.
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

      // GPS breadcrumb on top of course. Solid green so it visually
      // contrasts with the dashed-blue planned course — at a glance the
      // user can see "this is what I planned vs this is what I sailed".
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

      // Computed isochrone route — magenta, thinner than the track line
      // so the actual sailed track stays prominent during a race.
      map.addSource("route", { type: "geojson", data: emptyLine() });
      map.addLayer({
        id: "route-line",
        type: "line",
        source: "route",
        paint: {
          "line-color": "#c026d3",
          "line-width": 3,
          "line-opacity": 0.85,
        },
      });

      pushViewport();
      map.on("moveend", pushViewport);

      setStyleLoaded(true);
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
    // Only ever run once per mount. `base` is captured for the initial
    // center; subsequent base changes are handled by the flyTo effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Push base wind features whenever the base payload, the viewport, or
  // the active venue changes. Excludes points that fall inside the active
  // venue's bbox so we don't double-render where the high-res overlay
  // takes over.
  useEffect(() => {
    if (!styleLoaded || !baseWeather) return;
    const map = mapRef.current;
    const src = map.getSource("wind-base");
    if (!src) return;

    const features = computeFeatures(map, baseWeather, venue?.bbox ?? null);
    src.setData({ type: "FeatureCollection", features });
  }, [baseWeather, viewport, venue, styleLoaded]);

  // Push venue wind features whenever the venue payload or viewport
  // changes. When venue is null/data is null, push empty so any prior
  // overlay clears immediately.
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

  // Fly to the base region center when the base changes. Skipped if an
  // active race is set (the race's fitBounds wins) or if GPS will handle
  // precise centering. Only fires once per region transition.
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

  // Recenter on browser GPS when it resolves — but only once, and only
  // if there's no active race. An active race's fitBounds always wins.
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

  // Render / clear the active race's course + markers.
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

  // Push the recorder's breadcrumb to the `track` source whenever a new
  // point lands. Empty array clears the line — handles the
  // user-cleared-the-race case as well as the recorder-was-stopped case.
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

  // Push the computed isochrone route to the magenta line source whenever
  // the routing hook produces a new GeoJSON Feature. Empty-data path
  // clears the line on logout / race-cleared / fresh-recompute-error.
  useEffect(() => {
    if (!styleLoaded) return;
    const map = mapRef.current;
    if (!map) return;
    const src = map.getSource("route");
    if (!src) return;

    if (!routing.route) {
      src.setData(emptyLine());
      return;
    }
    src.setData(routing.route);
  }, [routing.route, styleLoaded]);

  return (
    <div style={styles.shell}>
      <div ref={containerRef} style={styles.map} />

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
        />
      )}

      {/* Wind status — top left. Shows the active source label and
          freshness. Region label is intentionally omitted — the map
          itself is the indicator. */}
      {baseWeather && (
        <div style={styles.windOverlay}>
          <span style={styles.label}>{baseSource.toUpperCase()}</span>
          <span style={styles.value}>
            valid {validTime?.toISOString().slice(11, 16)}Z
          </span>
          <span style={styles.age}>{ageMinutes}m old</span>
        </div>
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
}) {
  const cd = useCountdown(race.start_at);
  return (
    <div style={styles.raceOverlay}>
      <div style={styles.raceMain}>
        <div style={styles.raceLabel}>Active race</div>
        <div style={styles.raceName}>{race.name}</div>
        <div
          style={{
            ...styles.raceCountdown,
            color: cd.isUnset
              ? "var(--ink-3)"
              : cd.isPast
                ? "var(--ink-3)"
                : "#1a73e8",
          }}
        >
          {cd.isUnset
            ? "No start time set"
            : cd.isPast
              ? cd.label
              : `Starts in ${cd.label}`}
        </div>
        {recording && queueLength > 0 && (
          <div style={styles.queueHint}>
            {queueLength} pt{queueLength === 1 ? "" : "s"} pending
            {recorderError ? " · offline" : ""}
          </div>
        )}
        {!recording && recorderError && (
          <div style={styles.recordError}>{recorderError}</div>
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
        <button onClick={onEdit} style={styles.raceEditBtn}>
          Edit
        </button>
        <button
          onClick={onClear}
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

// ── Helpers ─────────────────────────────────────────────────────────

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

  // Wind status (top-left).
  windOverlay: {
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
    zIndex: 5,
  },
  label: { fontWeight: 600, letterSpacing: "0.05em" },
  value: { color: "#475569" },
  age: { color: "#94a3b8" },

  // Race overlay (top-center).
  raceOverlay: {
    position: "absolute",
    top: 12,
    left: "50%",
    transform: "translateX(-50%)",
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "10px 14px 10px 18px",
    background: "rgba(255, 255, 255, 0.96)",
    backdropFilter: "blur(8px)",
    borderRadius: 10,
    boxShadow: "0 2px 10px rgba(0, 0, 0, 0.08)",
    minWidth: 240,
    maxWidth: "calc(100vw - 140px)",
    zIndex: 5,
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
    color: "var(--ink-3)",
    textTransform: "uppercase",
    letterSpacing: "0.08em",
    fontWeight: 500,
  },
  raceName: {
    fontSize: 15,
    color: "var(--ink)",
    fontWeight: 500,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  raceCountdown: {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 13,
    fontVariantNumeric: "tabular-nums",
    fontWeight: 500,
  },
  queueHint: {
    fontSize: 11,
    color: "var(--ink-3)",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    marginTop: 2,
  },
  recordError: {
    fontSize: 11,
    color: "#b00020",
    marginTop: 2,
    maxWidth: 220,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  raceActions: {
    display: "flex",
    gap: 6,
    flexShrink: 0,
    alignItems: "center",
  },
  // Big finger-sized record button. 44px tall meets Apple HIG tap target;
  // padding generous so it doesn't feel cramped next to Edit/Clear.
  recordBtn: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    height: 44,
    minWidth: 80,
    padding: "0 14px",
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    fontSize: 13,
    color: "var(--ink)",
    cursor: "pointer",
    fontFamily: "inherit",
    fontWeight: 500,
  },
  recordBtnOn: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    height: 44,
    minWidth: 80,
    padding: "0 14px",
    border: "1px solid #b00020",
    background: "#fff5f6",
    borderRadius: "var(--r-sm)",
    fontSize: 13,
    color: "#b00020",
    cursor: "pointer",
    fontFamily: "inherit",
    fontWeight: 600,
  },
  // Solid red circle while recording (will pulse via CSS animation later).
  // Outline version when idle so the button reads as "ready to record".
  recordDot: {
    display: "inline-block",
    width: 10,
    height: 10,
    borderRadius: "50%",
    border: "2px solid #b00020",
    background: "transparent",
  },
  recordDotOn: {
    display: "inline-block",
    width: 10,
    height: 10,
    borderRadius: "50%",
    background: "#b00020",
    boxShadow: "0 0 0 2px rgba(176, 0, 32, 0.18)",
  },
  recordLabel: { fontVariantNumeric: "tabular-nums" },
  raceEditBtn: {
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    padding: "6px 12px",
    fontSize: 13,
    color: "var(--ink)",
    cursor: "pointer",
    fontFamily: "inherit",
  },
  raceClearBtn: {
    width: 30,
    height: 30,
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    fontSize: 14,
    color: "var(--ink-3)",
    cursor: "pointer",
    padding: 0,
    fontFamily: "inherit",
  },
};
