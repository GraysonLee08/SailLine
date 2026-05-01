// MapView — the always-mounted base layer of the app.
//
// Renders wind barbs (adaptive density) for the user's current region, and
// on top of that the course of the currently active race when one is set:
// a dashed blue line through the marks plus numbered draggable-style
// markers (these ones are read-only — drag/edit happens in RaceEditor).
//
// Region is auto-detected (GPS → IP → great_lakes fallback) by useRegion.
// When the region changes — either because detection completed, or because
// a race in a different region was loaded — the map flies to the new
// region's center and the wind data refetches automatically (useWeather
// keys on region+source).
//
// The race overlay (top-center) shows the race name and a live countdown;
// Edit jumps to the editor and ✕ clears the active race.

import { useEffect, useMemo, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

import { useWeather } from "../hooks/useWeather";
import { useGeolocation } from "../hooks/useGeolocation";
import { useCountdown } from "../hooks/useCountdown";
import { useRegion } from "../hooks/useRegion";
import { regionCenter } from "../lib/regions";
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
// stride when zoomed out (decimates the native HRRR grid) and the
// synthetic spacing when zoomed in (interpolates between native points).
//
// HRRR is regridded to ~0.1° (~11km at our latitudes), so a typical 4nm
// race course at zoom 13 contains 0–1 native points. Below the native
// resolution we synthesize intermediate barbs via bilinear interpolation
// in windBarb.bilerpUV — visually smoother but adding no information
// beyond ~11km. Honest meteorological caveat: the interpolated values
// are a presentation layer, not new data.
const TARGET_BARB_SPACING_PX = 70;

// Map zoom levels used at various stages.
//   REGION_FLY_ZOOM — overview of the resolved region (also used for initial
//                     map mount, so the first render is already in the right place)
//   GPS_FLY_ZOOM   — close-up around the user's reported position
const REGION_FLY_ZOOM = 7;
const GPS_FLY_ZOOM = 13;

// Padding for fitBounds when an active race is loaded. Generous (140px)
// so there's room around the course for the routing model output once it
// lands, and so the top-center race overlay never covers a mark.
// maxZoom: 12 keeps tight courses from zooming in absurdly close so the
// user sees the whole route in context rather than two marks filling the
// screen.
const COURSE_FIT_PADDING = 140;
const COURSE_FIT_MAX_ZOOM = 12;

/**
 * Compute the wind barb features to render at the current map view.
 *
 * Adaptive density: aims for ~constant on-screen barb spacing
 * (TARGET_BARB_SPACING_PX) regardless of zoom level.
 */
function computeFeatures(map, weather) {
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
        features.push(makeFeature(lon, lat, u[i][j], v[i][j]));
      }
    }
  } else {
    // Zoomed in: native grid is too sparse. Walk a synthetic grid at
    // targetDeg spacing and bilerp u/v at each point.
    //
    // Snap the start lat/lon to a multiple of targetDeg so the grid
    // doesn't shift while panning — keeps barb positions stable to the
    // eye instead of crawling.
    const startLat = Math.ceil(south / targetDeg) * targetDeg;
    const startLon = Math.ceil(west / targetDeg) * targetDeg;

    for (let lat = startLat; lat <= north; lat += targetDeg) {
      for (let lon = startLon; lon <= east; lon += targetDeg) {
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

export function MapView({ activeRace, onEditActive, onClearActive }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const courseMarkersRef = useRef([]);
  // Tracks which race ID we've already fitBounds'd to, so re-renders
  // (e.g. countdown tick triggering a new useMemo elsewhere) don't
  // hijack the user's panning every second.
  const fittedRaceIdRef = useRef(null);
  // One-shot guard for the initial GPS recenter — without it, an
  // active race that resolves AFTER GPS would get its fitBounds
  // overwritten when GPS later refires through a re-render.
  const gpsHandledRef = useRef(false);
  // Tracks which region we've already flown to, so the flyTo only fires
  // when the region actually changes (not on every re-render).
  const flownRegionRef = useRef(null);
  const [styleLoaded, setStyleLoaded] = useState(false);

  const region = useRegion(activeRace);
  const source = region.defaultSource;
  const { data: weather, validTime, ageMinutes } = useWeather(
    region.name,
    source,
  );
  const { position } = useGeolocation();

  // Initialize map once. Assigning mapRef.current synchronously (before
  // .on("load")) makes the strict-mode double-mount in dev bail on the
  // second pass, which would otherwise cancel the style request.
  //
  // Initial center is the resolved region — useRegion returns synchronously
  // from localStorage or DEFAULT_REGION, so we always have something here
  // and avoid a flash-of-wrong-region for users outside the Great Lakes.
  useEffect(() => {
    if (mapRef.current || !containerRef.current) return;

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: "mapbox://styles/mapbox/light-v11",
      center: regionCenter(region),
      zoom: REGION_FLY_ZOOM,
    });
    mapRef.current = map;
    // Record the initial region so the flyTo effect below only fires on
    // genuine region changes, not on first mount.
    flownRegionRef.current = region.name;

    map.on("load", () => {
      const images = generateBarbImages();
      Object.entries(images).forEach(([id, dataUrl]) => {
        const img = new Image(64, 64);
        img.onload = () => {
          if (!map.hasImage(id)) map.addImage(id, img);
        };
        img.src = dataUrl;
      });

      // Wind layer first.
      map.addSource("wind", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "wind-barbs",
        type: "symbol",
        source: "wind",
        layout: {
          "icon-image": ["concat", "barb-", ["get", "bucket"]],
          "icon-rotate": ["get", "dir"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-size": 0.8,
        },
      });

      // Course-line on top — visually consistent with RaceEditor (blue
      // dashed). Last-added wins for layer order on a Mapbox style, so
      // the course always draws over the barbs even when both are
      // populated.
      map.addSource("course", {
        type: "geojson",
        data: emptyLine(),
      });
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

      setStyleLoaded(true);
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // Recompute and push wind features whenever:
  //   - a new weather payload lands
  //   - the user pans or zooms (moveend covers both, fires once after
  //     the gesture settles, so no extra debouncing needed)
  useEffect(() => {
    if (!styleLoaded || !weather) return;
    const map = mapRef.current;
    const source = map.getSource("wind");
    if (!source) return;

    const update = () => {
      const features = computeFeatures(map, weather);
      source.setData({ type: "FeatureCollection", features });
    };

    update();
    map.on("moveend", update);
    return () => {
      map.off("moveend", update);
    };
  }, [weather, styleLoaded]);

  // Fly to the region center when the region changes. Skipped if an
  // active race is set (the race's fitBounds wins) and skipped if a GPS
  // flyTo is about to fire for a precise location (more useful than the
  // region center). Only fires once per region transition.
  useEffect(() => {
    if (!mapRef.current) return;
    if (flownRegionRef.current === region.name) return;
    flownRegionRef.current = region.name;

    if (activeRace) return; // race claim wins
    if (position) return;   // GPS will handle precise centering below

    mapRef.current.flyTo({
      center: regionCenter(region),
      zoom: REGION_FLY_ZOOM,
      duration: 1200,
    });
  }, [region, activeRace, position]);

  // Recenter on browser GPS when it resolves — but only once, and only
  // if there's no active race claiming the viewport. An active race's
  // fitBounds always wins.
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
  // syncKey changes whenever the marks themselves change (e.g. user
  // edited the race and came back); fittedRaceIdRef gates the initial
  // fitBounds so it only runs when we switch RACES, not on every
  // re-render of the same race.
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

    // Always tear down old markers first.
    courseMarkersRef.current.forEach((m) => m.remove());
    courseMarkersRef.current = [];

    const source = map.getSource("course");
    if (!source) return;

    if (!activeRace || !activeRace.marks?.length) {
      source.setData(emptyLine());
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

      // Read-only: no draggable. Edits happen in RaceEditor.
      const marker = new mapboxgl.Marker({ element: el })
        .setLngLat([mark.lon, mark.lat])
        .addTo(map);

      courseMarkersRef.current.push(marker);
    });

    source.setData({
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

  return (
    <div style={styles.shell}>
      <div ref={containerRef} style={styles.map} />

      {/* Race overlay — top center, only when a race is active. */}
      {activeRace && (
        <RaceOverlay
          race={activeRace}
          onEdit={onEditActive}
          onClear={onClearActive}
        />
      )}

      {/* Wind status — top left. Shows the active source label and
          freshness. Region label is intentionally not shown — the map
          itself is the indicator. */}
      {weather && (
        <div style={styles.windOverlay}>
          <span style={styles.label}>{source.toUpperCase()}</span>
          <span style={styles.value}>
            valid {validTime?.toISOString().slice(11, 16)}Z
          </span>
          <span style={styles.age}>{ageMinutes}m old</span>
        </div>
      )}
    </div>
  );
}

function RaceOverlay({ race, onEdit, onClear }) {
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
      </div>
      <div style={styles.raceActions}>
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
  raceActions: {
    display: "flex",
    gap: 6,
    flexShrink: 0,
  },
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
