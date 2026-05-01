// RaceEditor — full-screen map editor for a single race plan.
//
// Three ways to add marks:
//   1. Pick a MORF course preset (T1, O1, ...) → fills start/marks/finish.
//   2. Click on the map → drops an unnamed mark at that point.
//   3. Type lat/lon directly into a row's inputs (decimal or deg-min).
//
// Hovering a marker on the map shows a popup with its name, formatted
// coords, and (for library marks) the race-book description.
//
// Lat/lon input format (deg-min vs decimal) is user-toggleable and
// persisted to localStorage. Storage and API are always decimal degrees.

import { useEffect, useMemo, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import { apiFetch } from "./api";
import { BOAT_CLASSES } from "./lib/boatClasses";
import { COURSE_FAMILIES, buildCourseMarks } from "./lib/morfCourses";
import {
  formatLat,
  formatLon,
  formatLatInput,
  formatLonInput,
  formatDecimal,
  parseCoord,
} from "./lib/latlon";

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN;

const DEFAULT_CENTER = [-87.55, 41.85]; // Centered on SA7
const DEFAULT_ZOOM = 11;
const COORD_FORMAT_KEY = "sailline.coordFormat";

export default function RaceEditor({ raceId, onClose, onSaved }) {
  const isNew = !raceId;

  const [name, setName] = useState("");
  const [mode, setMode] = useState("inshore");
  const [boatClass, setBoatClass] = useState(BOAT_CLASSES[0]);
  const [marks, setMarks] = useState([]);

  // 'dm' = deg-decimal-min (sailor default), 'decimal' = decimal degrees.
  // Persisted so it sticks across sessions / races.
  const [coordFormat, setCoordFormat] = useState(() => {
    try {
      return localStorage.getItem(COORD_FORMAT_KEY) || "dm";
    } catch {
      return "dm";
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(COORD_FORMAT_KEY, coordFormat);
    } catch {
      /* localStorage disabled */
    }
  }, [coordFormat]);

  const [loading, setLoading] = useState(!isNew);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const setMarksRef = useRef(setMarks);
  setMarksRef.current = setMarks;

  // ── Load existing race ────────────────────────────────────────────
  useEffect(() => {
    if (isNew) return;
    let cancelled = false;
    apiFetch(`/api/races/${raceId}`)
      .then((race) => {
        if (cancelled) return;
        setName(race.name);
        setMode(race.mode);
        setBoatClass(race.boat_class);
        setMarks(race.marks || []);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e.message || String(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [raceId, isNew]);

  // ── Map init ──────────────────────────────────────────────────────
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const markersRef = useRef([]);
  const fittedRef = useRef(false);
  const [styleLoaded, setStyleLoaded] = useState(false);

  useEffect(() => {
    if (mapRef.current || !containerRef.current) return;

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: "mapbox://styles/mapbox/light-v11",
      center: DEFAULT_CENTER,
      zoom: DEFAULT_ZOOM,
    });
    mapRef.current = map;

    map.on("load", () => {
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
      setStyleLoaded(true);
    });

    map.on("click", (e) => {
      const { lng, lat } = e.lngLat;
      setMarksRef.current((prev) => [
        ...prev,
        { name: defaultMarkName(prev.length), lat, lon: lng },
      ]);
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // ── Sync markers + course line whenever positions/order change ────
  const syncKey = useMemo(
    () => marks.map((m) => `${m.name}|${m.lat}|${m.lon}`).join("~"),
    [marks],
  );

  useEffect(() => {
    if (!styleLoaded) return;
    const map = mapRef.current;
    if (!map) return;

    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];

    marks.forEach((mark, i) => {
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

      const marker = new mapboxgl.Marker({ element: el, draggable: true })
        .setLngLat([mark.lon, mark.lat])
        .addTo(map);

      marker.on("dragend", () => {
        const ll = marker.getLngLat();
        setMarksRef.current((prev) =>
          prev.map((m, idx) =>
            idx === i
              ? { ...m, lat: ll.lat, lon: ll.lng, description: undefined }
              : m,
          ),
        );
      });

      markersRef.current.push(marker);
    });

    const src = map.getSource("course");
    if (src) {
      src.setData({
        type: "Feature",
        properties: {},
        geometry: {
          type: "LineString",
          coordinates: marks.map((m) => [m.lon, m.lat]),
        },
      });
    }

    if (!fittedRef.current && marks.length > 0) {
      fitToMarks(map, marks);
      fittedRef.current = true;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncKey, styleLoaded]);

  // ── Mutations from sidebar ────────────────────────────────────────
  const moveUp = (i) =>
    setMarks((prev) => {
      if (i === 0) return prev;
      const next = [...prev];
      [next[i - 1], next[i]] = [next[i], next[i - 1]];
      return next;
    });

  const moveDown = (i) =>
    setMarks((prev) => {
      if (i === prev.length - 1) return prev;
      const next = [...prev];
      [next[i], next[i + 1]] = [next[i + 1], next[i]];
      return next;
    });

  const deleteMark = (i) =>
    setMarks((prev) => prev.filter((_, idx) => idx !== i));

  const renameMark = (i, value) =>
    setMarks((prev) =>
      prev.map((m, idx) => (idx === i ? { ...m, name: value } : m)),
    );

  const updateCoord = (i, field, value) =>
    setMarks((prev) =>
      prev.map((m, idx) =>
        idx === i ? { ...m, [field]: value, description: undefined } : m,
      ),
    );

  const addEmptyMark = () =>
    setMarks((prev) => [
      ...prev,
      { name: defaultMarkName(prev.length), lat: 0, lon: 0 },
    ]);

  const applyCourseTemplate = (courseId) => {
    if (!courseId) return;
    const next = buildCourseMarks(courseId);
    if (!next) {
      setError(`Unknown course template: ${courseId}`);
      return;
    }
    if (
      marks.length > 0 &&
      !confirm(
        `Replace your current ${marks.length} mark${marks.length === 1 ? "" : "s"} with course ${courseId}?`,
      )
    ) {
      return;
    }
    setMarks(next);
    fittedRef.current = false;
  };

  // ── Save ──────────────────────────────────────────────────────────
  const handleSave = async () => {
    if (!name.trim()) {
      setError("Give the race a name before saving.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload = {
        name: name.trim(),
        mode,
        boat_class: boatClass,
        marks: marks.map((m) => ({
          name: m.name,
          lat: m.lat,
          lon: m.lon,
          ...(m.description ? { description: m.description } : {}),
        })),
      };
      if (isNew) {
        await apiFetch("/api/races", { method: "POST", body: payload });
      } else {
        await apiFetch(`/api/races/${raceId}`, {
          method: "PATCH",
          body: payload,
        });
      }
      onSaved?.();
      onClose();
    } catch (e) {
      setError(e.message || String(e));
      setSaving(false);
    }
  };

  // ── Render ────────────────────────────────────────────────────────
  return (
    <div style={styles.shell}>
      <header style={styles.topBar}>
        <button onClick={onClose} style={styles.cancelBtn}>Cancel</button>
        <span style={styles.topTitle}>{isNew ? "New race" : "Edit race"}</span>
        <button
          onClick={handleSave}
          disabled={saving || loading}
          style={{ ...styles.saveBtn, opacity: saving || loading ? 0.6 : 1 }}
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </header>

      <div style={styles.workspace}>
        <div ref={containerRef} style={styles.map} />

        <aside style={styles.sidebar}>
          {error && <div style={styles.error}>{error}</div>}

          <div style={styles.scrollArea}>
            <Section label="Name">
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. MORF Saturday"
                style={styles.input}
              />
            </Section>

            <Section label="Mode">
              <div style={styles.radioGroup}>
                <ModeRadio value="inshore" current={mode} onChange={setMode}>Inshore</ModeRadio>
                <ModeRadio value="distance" current={mode} onChange={setMode}>Distance</ModeRadio>
              </div>
            </Section>

            <Section label="Boat class">
              <select
                value={boatClass}
                onChange={(e) => setBoatClass(e.target.value)}
                style={styles.input}
              >
                {BOAT_CLASSES.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </Section>

            <Section label="MORF course preset">
              <select
                value=""
                onChange={(e) => {
                  applyCourseTemplate(e.target.value);
                  e.target.value = "";
                }}
                style={styles.input}
              >
                <option value="">— Pick a course to load —</option>
                {COURSE_FAMILIES.map((fam) => (
                  <optgroup key={fam.family} label={fam.label}>
                    {fam.courses.map((id) => (
                      <option key={id} value={id}>{id}</option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </Section>

            <Section
              label={`Course (${marks.length})`}
              action={
                <FormatToggle value={coordFormat} onChange={setCoordFormat} />
              }
            >
              {marks.length === 0 ? (
                <p style={styles.hint}>
                  Pick a MORF course above, click anywhere on the map to drop a
                  mark, or add one and type its lat/lon below.
                </p>
              ) : (
                <ul style={styles.marksList}>
                  {marks.map((m, i) => (
                    <MarkRow
                      key={i}
                      index={i}
                      mark={m}
                      format={coordFormat}
                      isFirst={i === 0}
                      isLast={i === marks.length - 1}
                      onRename={(v) => renameMark(i, v)}
                      onLat={(v) => updateCoord(i, "lat", v)}
                      onLon={(v) => updateCoord(i, "lon", v)}
                      onUp={() => moveUp(i)}
                      onDown={() => moveDown(i)}
                      onDelete={() => deleteMark(i)}
                    />
                  ))}
                </ul>
              )}
              <button onClick={addEmptyMark} style={styles.addBtn}>
                + Add mark manually
              </button>
            </Section>
          </div>
        </aside>
      </div>

      {loading && <div style={styles.loadingOverlay}>Loading race…</div>}
    </div>
  );
}

// ── Mark row (local string state, commits on blur) ──────────────────

function MarkRow({
  index, mark, format, isFirst, isLast,
  onRename, onLat, onLon, onUp, onDown, onDelete,
}) {
  // Pick formatters based on the active format. parseCoord handles both
  // formats regardless, so users can still paste decimal into a deg-min
  // input (or vice versa) — we just re-format on commit.
  const fmtLat = format === "dm" ? formatLatInput : formatDecimal;
  const fmtLon = format === "dm" ? formatLonInput : formatDecimal;

  const [latStr, setLatStr] = useState(fmtLat(mark.lat));
  const [lonStr, setLonStr] = useState(fmtLon(mark.lon));

  // Resync local strings whenever the underlying value or the active
  // format changes (e.g. drag, course load, format toggle).
  useEffect(() => { setLatStr(fmtLat(mark.lat)); }, [mark.lat, format]);
  useEffect(() => { setLonStr(fmtLon(mark.lon)); }, [mark.lon, format]);

  const commit = (str, setStr, current, fmt, onCommit) => {
    const v = parseCoord(str);
    if (Number.isFinite(v)) {
      onCommit(v);
      setStr(fmt(v));
    } else {
      setStr(fmt(current));
    }
  };
  const onLatBlur = () => commit(latStr, setLatStr, mark.lat, fmtLat, onLat);
  const onLonBlur = () => commit(lonStr, setLonStr, mark.lon, fmtLon, onLon);
  const onKey = (e) => { if (e.key === "Enter") e.target.blur(); };

  const placeholderLat = format === "dm" ? "41 51.17 N" : "41.85283";
  const placeholderLon = format === "dm" ? "87 33.41 W" : "-87.55683";

  return (
    <li style={styles.markRow}>
      <div style={styles.markRowTop}>
        <span style={styles.markIdx}>{index + 1}</span>
        <input
          value={mark.name}
          onChange={(e) => onRename(e.target.value)}
          style={styles.markName}
          title={mark.description || ""}
        />
        <button onClick={onUp} disabled={isFirst} style={styles.iconBtn} title="Move up">↑</button>
        <button onClick={onDown} disabled={isLast} style={styles.iconBtn} title="Move down">↓</button>
        <button onClick={onDelete} style={styles.iconBtnDanger} title="Delete">×</button>
      </div>
      <div style={styles.markRowBottom}>
        <input
          value={latStr}
          onChange={(e) => setLatStr(e.target.value)}
          onBlur={onLatBlur}
          onKeyDown={onKey}
          placeholder={placeholderLat}
          style={styles.coordInput}
          title="Decimal (41.85283) or deg-min (41 51.17 N) both accepted"
        />
        <input
          value={lonStr}
          onChange={(e) => setLonStr(e.target.value)}
          onBlur={onLonBlur}
          onKeyDown={onKey}
          placeholder={placeholderLon}
          style={styles.coordInput}
          title="Decimal (-87.55683) or deg-min (87 33.41 W) both accepted"
        />
      </div>
    </li>
  );
}

// ── Subcomponents ───────────────────────────────────────────────────

function Section({ label, action, children }) {
  return (
    <div style={styles.section}>
      <div style={styles.sectionHeader}>
        <span style={styles.label}>{label}</span>
        {action}
      </div>
      {children}
    </div>
  );
}

function ModeRadio({ value, current, onChange, children }) {
  const checked = current === value;
  return (
    <label
      style={{
        ...styles.radio,
        borderColor: checked ? "var(--ink)" : "var(--rule)",
        background: checked ? "var(--ink)" : "var(--paper)",
        color: checked ? "var(--paper)" : "var(--ink)",
      }}
    >
      <input type="radio" checked={checked} onChange={() => onChange(value)} style={{ display: "none" }} />
      {children}
    </label>
  );
}

function FormatToggle({ value, onChange }) {
  return (
    <div style={styles.formatToggle} role="group" aria-label="Coordinate format">
      <FormatBtn active={value === "dm"} onClick={() => onChange("dm")}>Deg-min</FormatBtn>
      <FormatBtn active={value === "decimal"} onClick={() => onChange("decimal")}>Decimal</FormatBtn>
    </div>
  );
}
function FormatBtn({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      style={{
        ...styles.formatBtn,
        background: active ? "var(--ink)" : "var(--paper)",
        color: active ? "var(--paper)" : "var(--ink-3)",
      }}
    >
      {children}
    </button>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────

function defaultMarkName(index) {
  if (index === 0) return "Start";
  return `Mark ${index}`;
}

function emptyLine() {
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "LineString", coordinates: [] },
  };
}

function fitToMarks(map, marks) {
  if (marks.length === 1) {
    map.flyTo({ center: [marks[0].lon, marks[0].lat], zoom: 11, duration: 0 });
    return;
  }
  const bounds = new mapboxgl.LngLatBounds(
    [marks[0].lon, marks[0].lat],
    [marks[0].lon, marks[0].lat],
  );
  marks.forEach((m) => bounds.extend([m.lon, m.lat]));
  map.fitBounds(bounds, { padding: 100, duration: 0 });
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
    cursor: grab;
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

// ── Styles ──────────────────────────────────────────────────────────

const SIDEBAR_WIDTH = 380;

const styles = {
  shell: { position: "absolute", inset: 0, background: "var(--paper)", display: "flex", flexDirection: "column", overflow: "hidden" },
  topBar: { display: "flex", alignItems: "center", gap: 16, padding: "12px 20px", borderBottom: "1px solid var(--rule)", background: "var(--paper)", flexShrink: 0 },
  cancelBtn: { border: "1px solid var(--rule)", background: "var(--paper)", borderRadius: "var(--r-sm)", padding: "8px 14px", fontSize: 13, color: "var(--ink)", cursor: "pointer" },
  topTitle: { flex: 1, fontSize: 15, color: "var(--ink-3)" },
  saveBtn: { border: "none", background: "var(--ink)", color: "var(--paper)", borderRadius: "var(--r-md)", padding: "10px 22px", fontSize: 14, fontWeight: 500, cursor: "pointer" },
  workspace: { flex: 1, display: "flex", minHeight: 0 },
  map: { flex: 1, minWidth: 0 },
  sidebar: { width: SIDEBAR_WIDTH, flexShrink: 0, borderLeft: "1px solid var(--rule)", background: "var(--paper)", display: "flex", flexDirection: "column", overflow: "hidden" },
  scrollArea: { flex: 1, overflowY: "auto", padding: "20px" },
  section: { marginBottom: 18 },
  sectionHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: 8 },
  label: { fontSize: 11, color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 500 },
  input: { width: "100%", height: 40, padding: "0 12px", border: "1.5px solid var(--rule)", borderRadius: "var(--r-sm)", fontSize: 14, color: "var(--ink)", background: "var(--paper)", outline: "none", boxSizing: "border-box", fontFamily: "inherit" },
  radioGroup: { display: "flex", gap: 8 },
  radio: { flex: 1, height: 40, border: "1.5px solid var(--rule)", borderRadius: "var(--r-sm)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, cursor: "pointer", transition: "background 0.1s, border-color 0.1s, color 0.1s" },
  formatToggle: { display: "flex", border: "1px solid var(--rule)", borderRadius: "var(--r-sm)", overflow: "hidden" },
  formatBtn: { border: "none", padding: "4px 10px", fontSize: 11, fontFamily: "inherit", cursor: "pointer", textTransform: "uppercase", letterSpacing: "0.04em", fontWeight: 500 },
  hint: { fontSize: 13, color: "var(--ink-3)", margin: "0 0 12px", lineHeight: 1.5 },
  marksList: { listStyle: "none", padding: 0, margin: "0 0 12px" },
  markRow: { padding: "10px 0", borderBottom: "1px solid var(--rule)" },
  markRowTop: { display: "flex", alignItems: "center", gap: 6 },
  markRowBottom: { display: "flex", alignItems: "center", gap: 6, marginTop: 6, paddingLeft: 30 },
  markIdx: { width: 24, height: 24, borderRadius: "50%", background: "var(--ink)", color: "var(--paper)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, fontWeight: 600, flexShrink: 0 },
  markName: { flex: 1, minWidth: 0, height: 30, border: "1px solid transparent", background: "transparent", fontSize: 13, color: "var(--ink)", padding: "0 6px", borderRadius: "var(--r-sm)", outline: "none", fontFamily: "inherit" },
  coordInput: { flex: 1, minWidth: 0, height: 28, padding: "0 8px", border: "1px solid var(--rule)", borderRadius: "var(--r-sm)", fontSize: 12, fontFamily: "var(--mono, ui-monospace, monospace)", color: "var(--ink)", background: "var(--paper)", outline: "none", boxSizing: "border-box" },
  iconBtn: { width: 26, height: 26, border: "1px solid var(--rule)", background: "var(--paper)", borderRadius: "var(--r-sm)", fontSize: 14, color: "var(--ink)", cursor: "pointer", padding: 0, flexShrink: 0 },
  iconBtnDanger: { width: 26, height: 26, border: "1px solid var(--rule)", background: "var(--paper)", borderRadius: "var(--r-sm)", fontSize: 16, color: "var(--error)", cursor: "pointer", padding: 0, flexShrink: 0 },
  addBtn: { width: "100%", padding: "10px", border: "1px dashed var(--rule)", background: "transparent", borderRadius: "var(--r-sm)", fontSize: 13, color: "var(--ink-3)", cursor: "pointer", fontFamily: "inherit" },
  error: { padding: "10px 12px", margin: "20px 20px 0", background: "rgba(214, 59, 31, 0.08)", color: "var(--error)", borderRadius: "var(--r-sm)", fontSize: 13, border: "1px solid rgba(214, 59, 31, 0.2)" },
  loadingOverlay: { position: "absolute", inset: 0, background: "rgba(255,255,255,0.85)", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--ink-3)", fontSize: 14, pointerEvents: "auto" },
};
