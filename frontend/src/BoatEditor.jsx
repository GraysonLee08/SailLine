// BoatEditor — create or edit a boat. Form fields mirror the MWPHRF
// cert. The cert-upload button parses a PDF server-side and pre-fills
// the form with what came back; the user reviews and clicks Save.

import { useEffect, useMemo, useRef, useState } from "react";

import { apiFetch } from "./api";
import { useBoats } from "./hooks/useBoats";

const TEXT_FIELDS = [
  ["name",         "Name",          { required: true }],
  ["sail_number",  "Sail number"],
  ["yacht_type",   "Yacht type"],
  ["cert_number",  "Cert #"],
  ["engine",       "Engine"],
  ["prop_install", "Prop install"],
  ["prop_type",    "Prop type"],
];

const INT_FIELDS = [
  ["year",          "Year"],
  ["mwphrf_region", "MWPHRF Region"],
  ["hcp",           "HCP (ToD buoy, spin)"],
  ["dhcp",          "DHCP (ToD random leg, spin)"],
  ["nshcp",         "NSHCP (ToD buoy, non-spin)"],
  ["dnshcp",        "DNSHCP (ToD random leg, non-spin)"],
];

const FLOAT_FIELDS = [
  ["loa",          "LOA"],
  ["lwl",          "LWL"],
  ["beam",         "Beam"],
  ["draft",        "Draft"],
  ["displacement", "Displacement (lb)"],
  ["p",            "P (main luff)"],
  ["e",            "E (main foot)"],
  ["i",            "I (foretriangle height)"],
  ["j",            "J (foretriangle base)"],
  ["isp",          "ISP"],
  ["spl",          "SPL"],
  ["jc_tps",       "JC_TPS"],
];

const DATE_FIELDS = [
  ["cert_issued_on", "Cert issued on"],
];


export default function BoatEditor({ boatId, onClose, onSaved }) {
  const { create, update, uploadCert } = useBoats();
  const [form, setForm] = useState(() => emptyForm());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [parsedNotice, setParsedNotice] = useState(null);
  const fileInput = useRef(null);

  // Load existing boat if editing.
  useEffect(() => {
    if (!boatId) return;
    let cancelled = false;
    setLoading(true);
    apiFetch(`/api/boats/${boatId}`)
      .then((b) => {
        if (cancelled) return;
        setForm({ ...emptyForm(), ...b });
        setError(null);
      })
      .catch((e) => !cancelled && setError(e.message || String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [boatId]);

  const setField = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const handleSave = async (e) => {
    e?.preventDefault?.();
    setError(null);
    const payload = coerceForm(form);
    if (!payload.name) {
      setError("Name is required.");
      return;
    }
    setLoading(true);
    try {
      const saved = boatId
        ? await update(boatId, payload)
        : await create(payload);
      onSaved?.(saved);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  const handleCertUpload = async (file) => {
    if (!file) return;
    setError(null);
    setParsedNotice(null);
    if (!boatId) {
      setError("Save the boat first, then upload the cert.");
      return;
    }
    setLoading(true);
    try {
      const res = await uploadCert(boatId, file);
      if (!res.parse_succeeded) {
        setParsedNotice(
          "Couldn't recognise the PDF as an MWPHRF cert. " +
          "Fill the fields in manually.",
        );
        return;
      }
      // Pre-fill any field in `parsed` that the user hasn't already
      // touched. Conservative: don't overwrite non-empty entries.
      setForm((prev) => {
        const next = { ...prev };
        for (const [k, v] of Object.entries(res.parsed)) {
          const cur = next[k];
          if (cur == null || cur === "" || cur === 0) next[k] = v;
        }
        return next;
      });
      const fields = Object.keys(res.parsed).length;
      setParsedNotice(
        `Parsed ${fields} fields from the cert. Review and click Save.`,
      );
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={styles.shell}>
      <header style={styles.header}>
        <button onClick={onClose} style={styles.backBtn} aria-label="Cancel">
          ← Cancel
        </button>
        <h1 style={styles.title}>{boatId ? "Edit boat" : "New boat"}</h1>
        <button
          onClick={handleSave}
          style={styles.saveBtn}
          disabled={loading}
        >
          {loading ? "Saving…" : "Save"}
        </button>
      </header>

      <main style={styles.body}>
        {error && <div style={styles.error}>{error}</div>}
        {parsedNotice && <div style={styles.notice}>{parsedNotice}</div>}

        <section style={styles.section}>
          <div style={styles.sectionTitle}>Certificate (MWPHRF PDF)</div>
          <div style={styles.row}>
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf"
              style={styles.fileInput}
              onChange={(e) => handleCertUpload(e.target.files?.[0])}
              disabled={!boatId || loading}
            />
            <div style={styles.help}>
              {boatId
                ? "Upload your MWPHRF cert; we'll pre-fill the fields below."
                : "Save the boat first to enable cert upload."}
            </div>
          </div>
        </section>

        <FormSection title="Identity" form={form} setField={setField}
                     fields={TEXT_FIELDS.slice(0, 4)} kind="text" />
        <FormSection title="Handicaps (seconds per nautical mile)"
                     form={form} setField={setField}
                     fields={INT_FIELDS.slice(2)} kind="int" />
        <FormSection title="Hull" form={form} setField={setField}
                     fields={FLOAT_FIELDS.slice(0, 5)} kind="float" />
        <FormSection title="Drive train" form={form} setField={setField}
                     fields={TEXT_FIELDS.slice(4)} kind="text" />
        <FormSection title="Rig" form={form} setField={setField}
                     fields={FLOAT_FIELDS.slice(5)} kind="float" />
        <FormSection title="Metadata" form={form} setField={setField}
                     fields={INT_FIELDS.slice(0, 2)} kind="int" />
        <FormSection title="Cert dates" form={form} setField={setField}
                     fields={DATE_FIELDS} kind="date" />
      </main>
    </div>
  );
}


function FormSection({ title, fields, form, setField, kind }) {
  return (
    <section style={styles.section}>
      <div style={styles.sectionTitle}>{title}</div>
      <div style={styles.grid}>
        {fields.map(([k, label]) => (
          <label key={k} style={styles.field}>
            <span style={styles.fieldLabel}>{label}</span>
            <input
              type={inputTypeFor(kind)}
              step={kind === "float" ? "any" : undefined}
              value={form[k] ?? ""}
              onChange={(e) => setField(k, e.target.value)}
              style={styles.input}
            />
          </label>
        ))}
      </div>
    </section>
  );
}


function inputTypeFor(kind) {
  if (kind === "int" || kind === "float") return "number";
  if (kind === "date") return "date";
  return "text";
}


function emptyForm() {
  return {
    name: "",
    sail_number: "", yacht_type: "", year: "", mwphrf_region: "",
    loa: "", lwl: "", beam: "", draft: "", displacement: "",
    engine: "", prop_install: "", prop_type: "",
    p: "", e: "", i: "", j: "", isp: "", spl: "", jc_tps: "",
    hcp: "", dhcp: "", nshcp: "", dnshcp: "",
    cert_number: "", cert_issued_on: "",
  };
}


function coerceForm(form) {
  // Convert "" → null, number-strings → numbers. We send only the
  // fields the user actually filled in.
  const out = {};
  for (const [k, v] of Object.entries(form)) {
    if (v === "" || v == null) continue;
    if (INT_FIELDS.some(([fk]) => fk === k)) {
      const n = parseInt(v, 10);
      if (!Number.isNaN(n)) out[k] = n;
    } else if (FLOAT_FIELDS.some(([fk]) => fk === k)) {
      const n = parseFloat(v);
      if (!Number.isNaN(n)) out[k] = n;
    } else {
      out[k] = v;
    }
  }
  return out;
}


const styles = {
  shell: {
    position: "absolute",
    inset: 0,
    background: "var(--paper, #f8f8f7)",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 16,
    padding: "20px 32px",
    borderBottom: "1px solid var(--rule, #eaeaea)",
  },
  backBtn: {
    border: "1px solid var(--rule, #eaeaea)",
    background: "var(--paper, #fff)",
    borderRadius: 6,
    padding: "8px 14px",
    fontSize: 14,
    cursor: "pointer",
  },
  title: { margin: 0, fontSize: 24, flex: 1 },
  saveBtn: {
    border: "none",
    background: "var(--ink, #16161a)",
    color: "var(--paper, #fff)",
    borderRadius: 8,
    padding: "10px 20px",
    fontSize: 14,
    cursor: "pointer",
  },
  body: {
    flex: 1,
    overflow: "auto",
    padding: "16px 32px 40px",
    display: "flex",
    flexDirection: "column",
    gap: 18,
  },
  error: {
    padding: 12,
    border: "1px solid #f0c4c4",
    background: "#fdecec",
    color: "#8a1f1f",
    borderRadius: 8,
  },
  notice: {
    padding: 12,
    border: "1px solid #bcd6f7",
    background: "#eef4fd",
    color: "#1a4d8f",
    borderRadius: 8,
  },
  section: {
    background: "white",
    border: "1px solid var(--rule, #eaeaea)",
    borderRadius: 10,
    padding: "12px 16px 16px",
  },
  sectionTitle: {
    fontSize: 12,
    letterSpacing: "0.05em",
    textTransform: "uppercase",
    color: "#3a3a40",
    fontWeight: 600,
    marginBottom: 10,
  },
  row: { display: "flex", gap: 12, alignItems: "center" },
  fileInput: {
    flexShrink: 0,
  },
  help: { fontSize: 12, color: "#6a6a6f" },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
    gap: 10,
  },
  field: { display: "flex", flexDirection: "column", gap: 4 },
  fieldLabel: {
    fontSize: 11,
    color: "#6a6a6f",
    letterSpacing: "0.04em",
  },
  input: {
    padding: "8px 10px",
    border: "1px solid var(--rule, #d8d8de)",
    borderRadius: 6,
    fontSize: 14,
    fontFamily: "inherit",
  },
};
