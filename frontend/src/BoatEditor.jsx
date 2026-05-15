// BoatEditor — create or edit a boat. Form fields mirror the MWPHRF
// cert. The cert-upload button parses a PDF server-side and pre-fills
// the form with what came back; the user reviews and clicks Save.
//
// D3: when editing an existing boat AND the caller is the owner, a
// Crew section appears with member management + invite UI.

import { useEffect, useMemo, useRef, useState } from "react";

import { apiFetch } from "./api";
import { useBoats } from "./hooks/useBoats";
import { useCrew } from "./hooks/useCrew";

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


export default function BoatEditor({ boatId, onClose, onSaved, currentUid }) {
  const { create, update, uploadCert } = useBoats();
  const [form, setForm] = useState(() => emptyForm());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [parsedNotice, setParsedNotice] = useState(null);
  const fileInput = useRef(null);

  // D3: only the boat owner can edit. Non-owners (crew / viewer)
  // see the same fields read-only. Backend gates writes too; this is
  // purely UX. We use the boat's ``viewer_role`` field (returned by
  // GET /api/boats/{id}) to decide. New boats (no boatId yet) are
  // always editable.
  const isOwner = !boatId || (form.viewer_role
    ? form.viewer_role === "owner"
    : currentUid && form.owner_id === currentUid);

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
          ← {isOwner ? "Cancel" : "Back"}
        </button>
        <h1 style={styles.title}>
          {boatId ? (isOwner ? "Edit boat" : "View boat") : "New boat"}
        </h1>
        {isOwner ? (
          <button
            onClick={handleSave}
            style={styles.saveBtn}
            disabled={loading}
          >
            {loading ? "Saving…" : "Save"}
          </button>
        ) : (
          <span style={{
            ...styles.saveBtn,
            background: "transparent",
            color: "#6a6a6f",
            border: "1px solid #d8d8de",
            cursor: "default",
          }}>
            Read-only
          </span>
        )}
      </header>

      <main style={styles.body}>
        {error && <div style={styles.error}>{error}</div>}
        {parsedNotice && <div style={styles.notice}>{parsedNotice}</div>}

        {isOwner && (
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
        )}

        <FormSection title="Identity" form={form} setField={setField}
                     fields={TEXT_FIELDS.slice(0, 4)} kind="text"
                     disabled={!isOwner} />
        <FormSection title="Handicaps (seconds per nautical mile)"
                     form={form} setField={setField}
                     fields={INT_FIELDS.slice(2)} kind="int"
                     disabled={!isOwner} />
        <FormSection title="Hull" form={form} setField={setField}
                     fields={FLOAT_FIELDS.slice(0, 5)} kind="float"
                     disabled={!isOwner} />
        <FormSection title="Drive train" form={form} setField={setField}
                     fields={TEXT_FIELDS.slice(4)} kind="text"
                     disabled={!isOwner} />
        <FormSection title="Rig" form={form} setField={setField}
                     fields={FLOAT_FIELDS.slice(5)} kind="float"
                     disabled={!isOwner} />
        <FormSection title="Metadata" form={form} setField={setField}
                     fields={INT_FIELDS.slice(0, 2)} kind="int"
                     disabled={!isOwner} />
        <FormSection title="Cert dates" form={form} setField={setField}
                     fields={DATE_FIELDS} kind="date"
                     disabled={!isOwner} />

        {isOwner && <CrewSection boatId={boatId} ownerUid={currentUid} />}
      </main>
    </div>
  );
}


// ─── Crew section (owner-only) ──────────────────────────────────────


function CrewSection({ boatId, ownerUid }) {
  const {
    members, invites, loading, error,
    updateRole, removeMember, createInvite, revokeInvite,
  } = useCrew(boatId);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("crew");
  const [busy, setBusy] = useState(false);
  const [lastCreatedCode, setLastCreatedCode] = useState(null);
  const [actionError, setActionError] = useState(null);

  const handleInviteEmail = async () => {
    if (!inviteEmail) return;
    setBusy(true);
    setActionError(null);
    setLastCreatedCode(null);
    try {
      const inv = await createInvite({
        role: inviteRole, email: inviteEmail,
      });
      setLastCreatedCode({
        url: inv.accept_url,
        emailed: inv.emailed,
        email: inv.email,
      });
      setInviteEmail("");
    } catch (e) {
      setActionError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleGenerateCode = async () => {
    setBusy(true);
    setActionError(null);
    setLastCreatedCode(null);
    try {
      const inv = await createInvite({ role: inviteRole });
      setLastCreatedCode({ code: inv.code, url: inv.accept_url });
    } catch (e) {
      setActionError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const copyToClipboard = (text) => {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      navigator.clipboard.writeText(text).catch(() => {});
    }
  };

  return (
    <section style={crewStyles.section}>
      <div style={crewStyles.sectionTitle}>Crew</div>
      {error && <div style={crewStyles.errorRow}>{error}</div>}
      {actionError && <div style={crewStyles.errorRow}>{actionError}</div>}

      <ul style={crewStyles.memberList}>
        {(members || []).map((m) => (
          <li key={m.user_id} style={crewStyles.memberRow}>
            <div style={crewStyles.memberMain}>
              <CrewAvatar url={m.avatar_url} label={m.display_name || m.email || m.user_id} />
              <div style={crewStyles.memberLabelStack}>
                {/* D4: display_name → email → raw uid fallback chain.
                    Monospace only when we're falling all the way through
                    to the uid (it's an opaque identifier; everything else
                    is a human-readable name). */}
                {m.display_name ? (
                  <span style={crewStyles.memberName}>{m.display_name}</span>
                ) : m.email ? (
                  <span style={crewStyles.memberName}>{m.email}</span>
                ) : (
                  <code style={crewStyles.uid}>{m.user_id}</code>
                )}
                {m.display_name && m.email && (
                  <span style={crewStyles.memberSubtle}>{m.email}</span>
                )}
              </div>
              <span style={crewStyles.roleBadge(m.role)}>{m.role}</span>
            </div>
            {m.user_id !== ownerUid && (
              <div style={crewStyles.memberActions}>
                <select
                  value={m.role}
                  onChange={(e) =>
                    updateRole(m.user_id, e.target.value).catch((err) =>
                      setActionError(err.message),
                    )
                  }
                  style={crewStyles.roleSelect}
                  disabled={m.role === "owner"}
                >
                  {m.role === "owner" && <option value="owner">owner</option>}
                  <option value="crew">crew</option>
                  <option value="viewer">viewer</option>
                </select>
                <button
                  onClick={() =>
                    removeMember(m.user_id).catch((err) =>
                      setActionError(err.message),
                    )
                  }
                  style={crewStyles.removeBtn}
                >
                  Remove
                </button>
              </div>
            )}
          </li>
        ))}
        {members && members.length === 1 && (
          <li style={crewStyles.empty}>
            No crew yet — invite someone below.
          </li>
        )}
      </ul>

      <div style={crewStyles.inviteBox}>
        <div style={crewStyles.inviteRow}>
          <label style={crewStyles.smallLabel}>Role</label>
          <select
            value={inviteRole}
            onChange={(e) => setInviteRole(e.target.value)}
            style={crewStyles.input}
          >
            <option value="crew">crew (can record + view)</option>
            <option value="viewer">viewer (read only)</option>
          </select>
        </div>

        <div style={crewStyles.inviteRow}>
          <label style={crewStyles.smallLabel}>Email invite</label>
          <input
            type="email"
            placeholder="crew@example.com"
            value={inviteEmail}
            onChange={(e) => setInviteEmail(e.target.value)}
            style={crewStyles.input}
          />
          <button
            onClick={handleInviteEmail}
            disabled={busy || !inviteEmail}
            style={crewStyles.primaryBtn}
          >
            Send
          </button>
        </div>

        <div style={crewStyles.inviteRow}>
          <label style={crewStyles.smallLabel}>Or generate code</label>
          <button
            onClick={handleGenerateCode}
            disabled={busy}
            style={crewStyles.secondaryBtn}
          >
            Generate join code
          </button>
        </div>

        {lastCreatedCode && (
          <div style={crewStyles.codeNotice}>
            {lastCreatedCode.code ? (
              <>
                <div style={crewStyles.codeTitle}>Share this code:</div>
                <div style={crewStyles.codeRow}>
                  <code style={crewStyles.codeText}>
                    {lastCreatedCode.code}
                  </code>
                  <button
                    onClick={() => copyToClipboard(lastCreatedCode.code)}
                    style={crewStyles.copyBtn}
                  >
                    Copy
                  </button>
                </div>
                <div style={crewStyles.codeHint}>
                  Recipient pastes this on the Accept Invite screen,
                  or uses the full link:
                </div>
              </>
            ) : (
              <div style={crewStyles.codeTitle}>
                {lastCreatedCode.emailed
                  ? `Email sent to ${lastCreatedCode.email}.`
                  : `Email send failed — copy the link below and share it manually.`}
              </div>
            )}
            <div style={crewStyles.urlRow}>
              <code style={crewStyles.urlText}>
                {lastCreatedCode.url}
              </code>
              <button
                onClick={() => copyToClipboard(lastCreatedCode.url)}
                style={crewStyles.copyBtn}
              >
                Copy
              </button>
            </div>
          </div>
        )}
      </div>

      {invites && invites.length > 0 && (
        <div style={crewStyles.pendingBlock}>
          <div style={crewStyles.smallLabel}>Pending invites</div>
          <ul style={crewStyles.pendingList}>
            {invites.map((inv) => (
              <li key={inv.code} style={crewStyles.pendingRow}>
                <span style={crewStyles.roleBadge(inv.role)}>
                  {inv.role}
                </span>
                <code style={crewStyles.codeTextSmall}>
                  {inv.email || inv.code}
                </code>
                <button
                  onClick={() =>
                    revokeInvite(inv.code).catch((err) =>
                      setActionError(err.message),
                    )
                  }
                  style={crewStyles.removeBtn}
                >
                  Revoke
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}


// Tiny circular avatar for the crew row. Falls back to an initial-
// letter chip when the member hasn't uploaded a photo yet.
function CrewAvatar({ url, label }) {
  if (url) {
    return (
      <img
        src={url}
        alt=""
        style={crewStyles.avatar}
        onError={(e) => { e.currentTarget.style.display = "none"; }}
      />
    );
  }
  const initial = (label || "?").trim().charAt(0).toUpperCase();
  return <div style={crewStyles.avatarFallback}>{initial}</div>;
}


const crewStyles = {
  section: {
    background: "white",
    border: "1px solid var(--rule, #eaeaea)",
    borderRadius: 10,
    padding: "14px 16px 16px",
    display: "flex",
    flexDirection: "column",
    gap: 12,
  },
  sectionTitle: {
    fontSize: 12,
    letterSpacing: "0.05em",
    textTransform: "uppercase",
    color: "#3a3a40",
    fontWeight: 600,
  },
  errorRow: {
    padding: 8,
    border: "1px solid #f0c4c4",
    background: "#fdecec",
    color: "#8a1f1f",
    borderRadius: 6,
    fontSize: 12,
  },
  memberList: {
    listStyle: "none",
    margin: 0,
    padding: 0,
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  empty: {
    fontSize: 12, color: "#6a6a6f", padding: "8px 0",
  },
  memberRow: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
    padding: "8px 10px",
    background: "#f8f8f7",
    borderRadius: 6,
  },
  memberMain: { display: "flex", alignItems: "center", gap: 10, minWidth: 0, flex: 1 },
  memberLabelStack: {
    display: "flex",
    flexDirection: "column",
    minWidth: 0,
    flex: 1,
    gap: 1,
  },
  memberName: {
    fontSize: 13,
    color: "#16161a",
    fontWeight: 500,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  memberSubtle: {
    fontSize: 11,
    color: "#6a6a6f",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  avatar: {
    width: 28,
    height: 28,
    borderRadius: "50%",
    objectFit: "cover",
    background: "#eaeaea",
    flexShrink: 0,
  },
  avatarFallback: {
    width: 28,
    height: 28,
    borderRadius: "50%",
    background: "#16161a",
    color: "white",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 12,
    fontWeight: 600,
    flexShrink: 0,
  },
  uid: {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 11,
    color: "#3a3a40",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  roleBadge: (role) => ({
    fontSize: 10,
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    fontWeight: 600,
    border: "1px solid",
    borderRadius: 4,
    padding: "1px 6px",
    ...(role === "owner"
      ? { color: "#16161a", borderColor: "#16161a" }
      : role === "crew"
        ? { color: "#1a73e8", borderColor: "#1a73e8" }
        : { color: "#6a6a6f", borderColor: "#bcbcc2" }),
  }),
  memberActions: { display: "flex", gap: 6, alignItems: "center" },
  roleSelect: {
    fontSize: 12,
    padding: "4px 6px",
    border: "1px solid #d8d8de",
    borderRadius: 4,
  },
  removeBtn: {
    fontSize: 11,
    padding: "4px 8px",
    border: "1px solid #e0a0a0",
    background: "white",
    color: "#8a1f1f",
    borderRadius: 4,
    cursor: "pointer",
  },
  inviteBox: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
    padding: 12,
    border: "1px dashed #d8d8de",
    borderRadius: 8,
  },
  inviteRow: { display: "flex", alignItems: "center", gap: 8 },
  smallLabel: {
    fontSize: 11,
    color: "#6a6a6f",
    width: 110,
    flexShrink: 0,
  },
  input: {
    flex: 1,
    padding: "6px 10px",
    border: "1px solid #d8d8de",
    borderRadius: 6,
    fontSize: 13,
    fontFamily: "inherit",
  },
  primaryBtn: {
    padding: "6px 12px",
    background: "#16161a",
    color: "white",
    border: "none",
    borderRadius: 6,
    fontSize: 12,
    fontWeight: 500,
    cursor: "pointer",
  },
  secondaryBtn: {
    padding: "6px 12px",
    background: "white",
    color: "#16161a",
    border: "1px solid #d8d8de",
    borderRadius: 6,
    fontSize: 12,
    cursor: "pointer",
  },
  codeNotice: {
    padding: 10,
    background: "#eef4fd",
    border: "1px solid #bcd6f7",
    borderRadius: 6,
    fontSize: 12,
    color: "#1a4d8f",
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  codeTitle: { fontWeight: 600 },
  codeRow: { display: "flex", alignItems: "center", gap: 8 },
  codeText: {
    fontFamily: "ui-monospace, monospace",
    fontSize: 16,
    fontWeight: 600,
    padding: "4px 10px",
    background: "white",
    border: "1px solid #bcd6f7",
    borderRadius: 4,
    letterSpacing: "0.05em",
  },
  codeTextSmall: {
    fontFamily: "ui-monospace, monospace",
    fontSize: 12,
    color: "#3a3a40",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    flex: 1,
  },
  copyBtn: {
    padding: "4px 10px",
    background: "white",
    border: "1px solid #bcd6f7",
    color: "#1a4d8f",
    borderRadius: 4,
    fontSize: 11,
    cursor: "pointer",
  },
  codeHint: { fontSize: 11, color: "#1a4d8f", opacity: 0.85 },
  urlRow: { display: "flex", alignItems: "center", gap: 8 },
  urlText: {
    fontFamily: "ui-monospace, monospace",
    fontSize: 11,
    background: "white",
    padding: "4px 6px",
    border: "1px solid #bcd6f7",
    borderRadius: 4,
    flex: 1,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  pendingBlock: {
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  pendingList: {
    listStyle: "none",
    margin: 0,
    padding: 0,
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  pendingRow: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "6px 10px",
    background: "#f8f8f7",
    borderRadius: 6,
  },
};


function FormSection({ title, fields, form, setField, kind, disabled = false }) {
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
              disabled={disabled}
              readOnly={disabled}
              style={{
                ...styles.input,
                background: disabled ? "#f8f8f7" : "white",
                cursor: disabled ? "default" : "text",
              }}
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
