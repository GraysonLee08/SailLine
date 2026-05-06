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

import { useEffect, useMemo, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

import { useWeather } from "../hooks/useWeather";
import { useGeolocation } from "../hooks/useGeolocation";
import { useCountdown } from "../hooks/useCountdown";
import { useRegion } from "../hooks/useRegion";
import { useTrackRecorder } from "../hooks/useTrackRecorder";
import { useRouting } from "../hooks/useRouting";
import { useRouteNotifications } from "../hooks/useRouteNotifications";
import { ComputeRouteButton, RouteStatus } from "./RouteControls.jsx";
import { BetterRouteBanner } from "./BetterRouteBanner.jsx";
import { regionCenter, venueForPoint, VENUE_ZOOM_THRESHOLD } from "../lib/regions";
import { uvToSpeedDir, bilerpUV, generateBarbImages } from "../lib/windBarb";
import { formatLat, formatLon } from "../lib/latlon";

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN;

const TARGET_BARB_SPACING_PX = 70;
const REGION_FLY_ZOOM = 7;
const GPS_FLY_ZOOM = 13;
const COURSE_FIT_PADDING = 140;
const COURSE_FIT_MAX_ZOOM = 12;

const RACE_OVERLAY_TOP_DEFAULT = 12;
const RACE_OVERLAY_TOP_WITH_BANNER = 76;

function computeFeatures(map, weather, excludeBbox = null) {
  const { lats, lons, u, v } = weather;
  const zoom = map.getZoom();
  const bounds = map.getBounds();
  const centerLat = map.getCenter().lat;

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

  const recorder = useTrackRecorder(activeRace?.id ?? null);
  const routing = useRouting(activeRace?.id ?? null);
  const notif = useRouteNotifications(activeRace?.id ?? null);

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
      return;
    }
    src.setData(routing.route);
  }, [routing.route, styleLoaded]);

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
}) {
  const cd = useCountdown(race.start_at);
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
};
