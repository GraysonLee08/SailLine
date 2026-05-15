// ProfileView — user profile editor.
//
// Two entry points:
//
//   1. **Forced first visit.** When AppView's /me fetch returns
//      ``profile_complete === false``, the user lands here with no
//      way out except submitting a display name. ``forced`` prop
//      controls the back/cancel affordances and the headline copy.
//
//   2. **Settings.** From the menu drawer; back button returns to
//      the map.
//
// All fields except display_name are optional. Saving PATCHes
// /api/users/me and hands the updated profile up to AppView via
// onSaved so the in-memory cache stays consistent.
//
// Avatar upload is a separate endpoint (POST /api/users/me/avatar
// multipart) — we don't try to bundle it into the PATCH because the
// backend needs the raw bytes and resizing happens server-side. The
// view shows an upload button and a "Remove" button when an avatar
// exists.

import { useEffect, useRef, useState } from "react";

import { apiFetch } from "./api";
import { auth } from "./firebase";

const CATEGORIES = [
  ["", "—"],
  ["group_1", "Group 1 (amateur)"],
  ["group_2", "Group 2"],
  ["group_3", "Group 3 (pro)"],
];


export default function ProfileView({ profile, forced = false, onSaved, onCancel }) {
  // Form mirrors the profile shape. Initialised from the prop and
  // never reset by prop changes — once the user starts typing we own
  // the buffer until they save or cancel.
  const [form, setForm] = useState(() => normaliseForm(profile));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [avatarError, setAvatarError] = useState(null);
  const fileInputRef = useRef(null);

  // When the parent re-fetches the profile (e.g. after avatar upload)
  // we want to reflect the new avatar_url without clobbering in-
  // progress edits. Only sync ``avatar_url`` from the prop.
  useEffect(() => {
    setForm((f) => ({ ...f, avatar_url: profile?.avatar_url ?? null }));
  }, [profile?.avatar_url]);

  const setField = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const handleSave = async (e) => {
    e?.preventDefault?.();
    setError(null);
    const displayName = (form.display_name || "").trim();
    if (!displayName) {
      setError("A display name is required so your crew can recognise you.");
      return;
    }
    setSaving(true);
    try {
      // Build the PATCH body: send only fields the user could have
      // changed. Empty strings become null so backend clears the field.
      const body = {
        display_name: displayName,
        phone: emptyToNull(form.phone),
        bio: emptyToNull(form.bio),
        weight_lb: numericOrNull(form.weight_lb),
        emergency_contact_name: emptyToNull(form.emergency_contact_name),
        emergency_contact_phone: emptyToNull(form.emergency_contact_phone),
        world_sailing_sailor_id: emptyToNull(form.world_sailing_sailor_id),
        world_sailing_category: form.world_sailing_category || null,
        safety_at_sea_cert_expiry: emptyToNull(form.safety_at_sea_cert_expiry),
      };
      const updated = await apiFetch("/api/users/me", {
        method: "PATCH",
        body,
      });
      onSaved?.(updated);
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleAvatarPick = () => fileInputRef.current?.click();

  const handleAvatarFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";  // allow re-uploading the same file
    if (!file) return;
    setAvatarBusy(true);
    setAvatarError(null);
    try {
      // Manual fetch — apiFetch always JSON-encodes the body, which
      // doesn't work for multipart. Same auth header pattern.
      const user = auth.currentUser;
      if (!user) throw new Error("Not authenticated");
      const token = await user.getIdToken();
      const fd = new FormData();
      fd.append("file", file);
      const apiBase = import.meta.env.VITE_API_URL || "";
      const res = await fetch(`${apiBase}/api/users/me/avatar`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: fd,
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`Upload failed (${res.status}): ${text || res.statusText}`);
      }
      const updated = await res.json();
      setField("avatar_url", updated.avatar_url);
      onSaved?.(updated);
    } catch (err) {
      setAvatarError(err.message || String(err));
    } finally {
      setAvatarBusy(false);
    }
  };

  const handleAvatarRemove = async () => {
    setAvatarBusy(true);
    setAvatarError(null);
    try {
      const updated = await apiFetch("/api/users/me/avatar", {
        method: "DELETE",
      });
      setField("avatar_url", null);
      onSaved?.(updated);
    } catch (err) {
      setAvatarError(err.message || String(err));
    } finally {
      setAvatarBusy(false);
    }
  };

  return (
    <div style={styles.shell}>
      <header style={styles.header}>
        <div>
          <h1 style={styles.title}>
            {forced ? "Set up your profile" : "Profile"}
          </h1>
          <p style={styles.subtitle}>
            {forced
              ? "Tell us how to address you so your crew sees something better than a Firebase ID."
              : "Your name, photo, and the sailor info distance-race entries ask for."}
          </p>
        </div>
        {!forced && onCancel && (
          <button onClick={onCancel} style={styles.backBtn} disabled={saving}>
            ← Back
          </button>
        )}
      </header>

      <form onSubmit={handleSave} style={styles.form}>
        {/* ─── Profile section ─────────────────────────────────── */}
        <section style={styles.section}>
          <div style={styles.sectionTitle}>Profile</div>

          <div style={styles.avatarRow}>
            <AvatarPreview url={form.avatar_url} name={form.display_name} />
            <div style={styles.avatarActions}>
              <button
                type="button"
                onClick={handleAvatarPick}
                disabled={avatarBusy}
                style={styles.secondaryBtn}
              >
                {avatarBusy
                  ? "Working…"
                  : form.avatar_url
                    ? "Change photo"
                    : "Upload photo"}
              </button>
              {form.avatar_url && (
                <button
                  type="button"
                  onClick={handleAvatarRemove}
                  disabled={avatarBusy}
                  style={styles.linkBtn}
                >
                  Remove
                </button>
              )}
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                onChange={handleAvatarFile}
                style={{ display: "none" }}
              />
            </div>
          </div>
          {avatarError && <div style={styles.errorRow}>{avatarError}</div>}

          <Field label="Email">
            <input
              type="email"
              value={profile?.email || ""}
              readOnly
              style={{ ...styles.input, background: "#f3f3f4", color: "#5a5a60" }}
            />
          </Field>

          <Field label="Display name" required>
            <input
              type="text"
              value={form.display_name || ""}
              onChange={(e) => setField("display_name", e.target.value)}
              placeholder="Grayson V."
              maxLength={80}
              style={styles.input}
              autoFocus={forced}
            />
          </Field>

          <Field label="Phone (optional)">
            <input
              type="tel"
              value={form.phone || ""}
              onChange={(e) => setField("phone", e.target.value)}
              placeholder="312-555-0100"
              style={styles.input}
            />
          </Field>

          <Field label="Bio (optional)" hint={`${(form.bio || "").length}/1000`}>
            <textarea
              value={form.bio || ""}
              onChange={(e) => setField("bio", e.target.value.slice(0, 1000))}
              placeholder="A line or two about your sailing — boats you've raced, positions you fill best…"
              rows={4}
              style={{ ...styles.input, fontFamily: "inherit", resize: "vertical" }}
            />
          </Field>
        </section>

        {/* ─── Sailing & safety ──────────────────────────────────── */}
        <section style={styles.section}>
          <div style={styles.sectionTitle}>Sailing &amp; safety</div>
          <p style={styles.sectionHint}>
            Optional — pre-fills distance-race entries (Chicago Mac,
            Bayview Mac, Bermuda) so you only enter this stuff once.
          </p>

          <Field label="Weight (lb)">
            <input
              type="number"
              min="50"
              max="500"
              step="0.5"
              value={form.weight_lb ?? ""}
              onChange={(e) => setField("weight_lb", e.target.value)}
              placeholder="185"
              style={styles.input}
            />
          </Field>

          <Field label="Emergency contact name">
            <input
              type="text"
              value={form.emergency_contact_name || ""}
              onChange={(e) => setField("emergency_contact_name", e.target.value)}
              style={styles.input}
            />
          </Field>

          <Field label="Emergency contact phone">
            <input
              type="tel"
              value={form.emergency_contact_phone || ""}
              onChange={(e) => setField("emergency_contact_phone", e.target.value)}
              style={styles.input}
            />
          </Field>

          <Field label="World Sailing sailor ID">
            <input
              type="text"
              value={form.world_sailing_sailor_id || ""}
              onChange={(e) => setField("world_sailing_sailor_id", e.target.value)}
              placeholder="USA12345"
              maxLength={32}
              style={styles.input}
            />
          </Field>

          <Field label="World Sailing category">
            <select
              value={form.world_sailing_category || ""}
              onChange={(e) => setField("world_sailing_category", e.target.value)}
              style={styles.input}
            >
              {CATEGORIES.map(([v, label]) => (
                <option key={v} value={v}>{label}</option>
              ))}
            </select>
          </Field>

          <Field label="Safety-at-Sea cert expiry">
            <input
              type="date"
              value={form.safety_at_sea_cert_expiry || ""}
              onChange={(e) => setField("safety_at_sea_cert_expiry", e.target.value)}
              style={styles.input}
            />
          </Field>
        </section>

        {error && <div style={styles.errorRow}>{error}</div>}

        <div style={styles.footer}>
          <button
            type="submit"
            disabled={saving}
            style={styles.primaryBtn}
          >
            {saving ? "Saving…" : forced ? "Continue" : "Save profile"}
          </button>
        </div>
      </form>
    </div>
  );
}


// ─── Helpers ─────────────────────────────────────────────────────────


function normaliseForm(profile) {
  // Pull every field we care about into the form state, defaulting
  // missing keys to empty so controlled inputs don't switch modes.
  if (!profile) {
    return {
      display_name: "", phone: "", bio: "", avatar_url: null,
      weight_lb: "", emergency_contact_name: "", emergency_contact_phone: "",
      world_sailing_sailor_id: "", world_sailing_category: "",
      safety_at_sea_cert_expiry: "",
    };
  }
  return {
    display_name: profile.display_name || "",
    phone: profile.phone || "",
    bio: profile.bio || "",
    avatar_url: profile.avatar_url || null,
    weight_lb: profile.weight_lb ?? "",
    emergency_contact_name: profile.emergency_contact_name || "",
    emergency_contact_phone: profile.emergency_contact_phone || "",
    world_sailing_sailor_id: profile.world_sailing_sailor_id || "",
    world_sailing_category: profile.world_sailing_category || "",
    safety_at_sea_cert_expiry: profile.safety_at_sea_cert_expiry || "",
  };
}


function emptyToNull(v) {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s ? s : null;
}


function numericOrNull(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}


function Field({ label, hint, required, children }) {
  return (
    <label style={styles.field}>
      <span style={styles.label}>
        {label}
        {required && <span style={styles.required}> *</span>}
        {hint && <span style={styles.hint}> {hint}</span>}
      </span>
      {children}
    </label>
  );
}


function AvatarPreview({ url, name }) {
  if (url) {
    return (
      <img
        src={url}
        alt="Profile"
        style={styles.avatarImg}
      />
    );
  }
  // Initial-letter fallback so the slot doesn't look like a bug
  // before the user uploads.
  const initial = (name || "").trim().charAt(0).toUpperCase() || "?";
  return <div style={styles.avatarFallback}>{initial}</div>;
}


const styles = {
  shell: {
    position: "absolute",
    inset: 0,
    background: "var(--paper, #fafaf8)",
    overflow: "auto",
    padding: "32px 24px 64px",
    boxSizing: "border-box",
  },
  header: {
    maxWidth: 640,
    margin: "0 auto 24px",
    display: "flex",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 16,
  },
  title: {
    margin: 0,
    fontSize: 24,
    fontWeight: 600,
    color: "#16161a",
  },
  subtitle: {
    margin: "6px 0 0",
    fontSize: 13,
    color: "#6a6a6f",
    maxWidth: 520,
    lineHeight: 1.4,
  },
  backBtn: {
    flexShrink: 0,
    padding: "6px 12px",
    background: "white",
    border: "1px solid #d8d8de",
    borderRadius: 6,
    fontSize: 13,
    cursor: "pointer",
    fontFamily: "inherit",
  },
  form: {
    maxWidth: 640,
    margin: "0 auto",
    display: "flex",
    flexDirection: "column",
    gap: 16,
  },
  section: {
    background: "white",
    border: "1px solid var(--rule, #eaeaea)",
    borderRadius: 10,
    padding: "16px 18px 18px",
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
  sectionHint: {
    margin: 0,
    fontSize: 12,
    color: "#6a6a6f",
    lineHeight: 1.4,
  },
  avatarRow: {
    display: "flex",
    alignItems: "center",
    gap: 16,
  },
  avatarImg: {
    width: 80,
    height: 80,
    borderRadius: "50%",
    objectFit: "cover",
    background: "#eaeaea",
  },
  avatarFallback: {
    width: 80,
    height: 80,
    borderRadius: "50%",
    background: "#16161a",
    color: "white",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 30,
    fontWeight: 600,
    fontFamily: "inherit",
  },
  avatarActions: {
    display: "flex",
    flexDirection: "column",
    gap: 6,
    alignItems: "flex-start",
  },
  field: {
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  label: {
    fontSize: 12,
    color: "#3a3a40",
    fontWeight: 500,
  },
  required: {
    color: "#c0392b",
  },
  hint: {
    color: "#9a9aa0",
    fontWeight: 400,
    fontSize: 11,
  },
  input: {
    padding: "8px 10px",
    border: "1px solid #d8d8de",
    borderRadius: 6,
    fontSize: 14,
    fontFamily: "inherit",
    background: "white",
    color: "#16161a",
    boxSizing: "border-box",
    width: "100%",
  },
  errorRow: {
    padding: 10,
    border: "1px solid #f0c4c4",
    background: "#fdecec",
    color: "#8a1f1f",
    borderRadius: 6,
    fontSize: 13,
  },
  footer: {
    display: "flex",
    justifyContent: "flex-end",
    gap: 8,
    marginTop: 4,
  },
  primaryBtn: {
    padding: "10px 18px",
    background: "#16161a",
    color: "white",
    border: "none",
    borderRadius: 6,
    fontSize: 14,
    fontWeight: 500,
    cursor: "pointer",
    fontFamily: "inherit",
  },
  secondaryBtn: {
    padding: "6px 12px",
    background: "white",
    color: "#16161a",
    border: "1px solid #d8d8de",
    borderRadius: 6,
    fontSize: 12,
    cursor: "pointer",
    fontFamily: "inherit",
  },
  linkBtn: {
    background: "none",
    border: "none",
    color: "#8a1f1f",
    fontSize: 11,
    cursor: "pointer",
    padding: 0,
    fontFamily: "inherit",
    textDecoration: "underline",
  },
};
