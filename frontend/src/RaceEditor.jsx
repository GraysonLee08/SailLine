// RaceEditor — full-screen map editor for a single race plan.
//
// Click anywhere on the map to drop a numbered mark; drag a marker to
// reposition; reorder/rename/delete in the sidebar. First mark is the
// effective start, last mark is the effective finish — that convention
// stays implicit for now to keep the v1 form simple. We can promote
// `kind: "start" | "mark" | "finish"` if/when start lines need two pins.
//
// State is flat: `marks` is the source of truth, Mapbox markers are
// derived. The sync effect keys on positions+order so renames don't
// thrash the markers.

import { useEffect, useMemo, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import { apiFetch } from "./api";
import { BOAT_CLASSES } from "./lib/boatClasses";

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN;

const DEFAULT_CENTER = [-87.0, 43.5]; // Lake Michigan
const DEFAULT_ZOOM = 8;

export default function RaceEditor({ raceId, onClose, onSaved }) {
  const isNew = !raceId;

  const [name, setName] = useState("");
  const [mode, setMode] = useState("inshore");
  const [boatClass, setBoatClass] = useState(BOAT_CLASSES[0]);
  const [marks, setMarks] = useState([]);

  const [loading, setLoading] = useState(!isNew);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Marker click handlers close over `marks` setter; ref keeps a stable
  // reference so we don't have to rebuild the map on every state change.
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
  // Keying on position+order (not the full marks object) means renames
  // don't re-create every marker on every keystroke.
  const positionKey = useMemo(
    () => marks.map((m) => `${m.lat},${m.lon}`).join("|"),
    [marks],
  );

  useEffect(() => {
    if (!styleLoaded) return;
    const map = mapRef.current;
    if (!map) return;

    // Tear down existing markers
    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];

    // Rebuild
    marks.forEach((mark, i) => {
      const el = createMarkerElement(i + 1);
      const marker = new mapboxgl.Marker({ element: el, draggable: true })
        .setLngLat([mark.lon, mark.lat])
        .addTo(map);

      marker.on("dragend", () => {
        const ll = marker.getLngLat();
        setMarksRef.current((prev) =>
          prev.map((m, idx) =>
            idx === i ? { ...m, lat: ll.lat, lon: ll.lng } : m,
          ),
        );
      });

      markersRef.current.push(marker);
    });

    // Course line
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

    // First-time fit-to-bounds when loading an existing race.
    if (!fittedRef.current && marks.length > 0) {
      if (marks.length === 1) {
        map.flyTo({ center: [marks[0].lon, marks[0].lat], zoom: 11, duration: 0 });
      } else {
        const bounds = new mapboxgl.LngLatBounds(
          [marks[0].lon, marks[0].lat],
          [marks[0].lon, marks[0].lat],
        );
        marks.forEach((m) => bounds.extend([m.lon, m.lat]));
        map.fitBounds(bounds, { padding: 100, duration: 0 });
      }
      fittedRef.current = true;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [positionKey, styleLoaded]);

  // ── Sidebar list mutations ────────────────────────────────────────
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
        marks: marks.map((m) => ({ name: m.name, lat: m.lat, lon: m.lon })),
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
        <button onClick={onClose} style={styles.cancelBtn}>
          Cancel
        </button>
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
              <ModeRadio value="inshore" current={mode} onChange={setMode}>
                Inshore
              </ModeRadio>
              <ModeRadio value="distance" current={mode} onChange={setMode}>
                Distance
              </ModeRadio>
            </div>
          </Section>

          <Section label="Boat class">
            <select
              value={boatClass}
              onChange={(e) => setBoatClass(e.target.value)}
              style={styles.input}
            >
              {BOAT_CLASSES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </Section>

          <Section label={`Course (${marks.length})`} flex>
            {marks.length === 0 ? (
              <p style={styles.hint}>
                Click anywhere on the map to drop the first mark.
              </p>
            ) : (
              <ul style={styles.marksList}>
                {marks.map((m, i) => (
                  <li key={i} style={styles.markRow}>
                    <span style={styles.markIdx}>{i + 1}</span>
                    <input
                      value={m.name}
                      onChange={(e) => renameMark(i, e.target.value)}
                      style={styles.markName}
                    />
                    <button
                      onClick={() => moveUp(i)}
                      disabled={i === 0}
                      style={styles.iconBtn}
                      title="Move up"
                    >
                      ↑
                    </button>
                    <button
                      onClick={() => moveDown(i)}
                      disabled={i === marks.length - 1}
                      style={styles.iconBtn}
                      title="Move down"
                    >
                      ↓
                    </button>
                    <button
                      onClick={() => deleteMark(i)}
                      style={styles.iconBtnDanger}
                      title="Delete"
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </aside>
      </div>

      {loading && <div style={styles.loadingOverlay}>Loading race…</div>}
    </div>
  );
}

// ── Subcomponents ───────────────────────────────────────────────────

function Section({ label, flex, children }) {
  return (
    <div
      style={{
        ...styles.section,
        ...(flex ? { flex: 1, minHeight: 0, display: "flex", flexDirection: "column" } : {}),
      }}
    >
      <span style={styles.label}>{label}</span>
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
      <input
        type="radio"
        checked={checked}
        onChange={() => onChange(value)}
        style={{ display: "none" }}
      />
      {children}
    </label>
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

// ── Styles ──────────────────────────────────────────────────────────

const SIDEBAR_WIDTH = 360;

const styles = {
  shell: {
    position: "absolute",
    inset: 0,
    background: "var(--paper)",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  topBar: {
    display: "flex",
    alignItems: "center",
    gap: 16,
    padding: "12px 20px",
    borderBottom: "1px solid var(--rule)",
    background: "var(--paper)",
    flexShrink: 0,
  },
  cancelBtn: {
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    padding: "8px 14px",
    fontSize: 13,
    color: "var(--ink)",
    cursor: "pointer",
  },
  topTitle: {
    flex: 1,
    fontSize: 15,
    color: "var(--ink-3)",
  },
  saveBtn: {
    border: "none",
    background: "var(--ink)",
    color: "var(--paper)",
    borderRadius: "var(--r-md)",
    padding: "10px 22px",
    fontSize: 14,
    fontWeight: 500,
    cursor: "pointer",
  },
  workspace: {
    flex: 1,
    display: "flex",
    minHeight: 0,
  },
  map: {
    flex: 1,
    minWidth: 0,
  },
  sidebar: {
    width: SIDEBAR_WIDTH,
    flexShrink: 0,
    borderLeft: "1px solid var(--rule)",
    background: "var(--paper)",
    display: "flex",
    flexDirection: "column",
    padding: "20px 20px 0",
    overflow: "hidden",
  },
  section: {
    marginBottom: 18,
  },
  label: {
    display: "block",
    fontSize: 11,
    color: "var(--ink-3)",
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    marginBottom: 8,
    fontWeight: 500,
  },
  input: {
    width: "100%",
    height: 40,
    padding: "0 12px",
    border: "1.5px solid var(--rule)",
    borderRadius: "var(--r-sm)",
    fontSize: 14,
    color: "var(--ink)",
    background: "var(--paper)",
    outline: "none",
    boxSizing: "border-box",
    fontFamily: "inherit",
  },
  radioGroup: {
    display: "flex",
    gap: 8,
  },
  radio: {
    flex: 1,
    height: 40,
    border: "1.5px solid var(--rule)",
    borderRadius: "var(--r-sm)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 14,
    cursor: "pointer",
    transition: "background 0.1s, border-color 0.1s, color 0.1s",
  },
  hint: {
    fontSize: 13,
    color: "var(--ink-3)",
    margin: 0,
    lineHeight: 1.5,
  },
  marksList: {
    listStyle: "none",
    padding: 0,
    margin: 0,
    overflowY: "auto",
    flex: 1,
    paddingBottom: 16,
  },
  markRow: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    padding: "6px 0",
    borderBottom: "1px solid var(--rule)",
  },
  markIdx: {
    width: 24,
    height: 24,
    borderRadius: "50%",
    background: "var(--ink)",
    color: "var(--paper)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 11,
    fontWeight: 600,
    flexShrink: 0,
  },
  markName: {
    flex: 1,
    minWidth: 0,
    height: 30,
    border: "1px solid transparent",
    background: "transparent",
    fontSize: 13,
    color: "var(--ink)",
    padding: "0 6px",
    borderRadius: "var(--r-sm)",
    outline: "none",
    fontFamily: "inherit",
  },
  iconBtn: {
    width: 26,
    height: 26,
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    fontSize: 14,
    color: "var(--ink)",
    cursor: "pointer",
    padding: 0,
    flexShrink: 0,
  },
  iconBtnDanger: {
    width: 26,
    height: 26,
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    fontSize: 16,
    color: "var(--error)",
    cursor: "pointer",
    padding: 0,
    flexShrink: 0,
  },
  error: {
    padding: "10px 12px",
    background: "rgba(214, 59, 31, 0.08)",
    color: "var(--error)",
    borderRadius: "var(--r-sm)",
    marginBottom: 16,
    fontSize: 13,
    border: "1px solid rgba(214, 59, 31, 0.2)",
  },
  loadingOverlay: {
    position: "absolute",
    inset: 0,
    background: "rgba(255,255,255,0.85)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    color: "var(--ink-3)",
    fontSize: 14,
    pointerEvents: "auto",
  },
};
