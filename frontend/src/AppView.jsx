// AppView — post-login. Reuses the split-screen aesthetic so the brand
// doesn't break across auth state.
//
// Right side now shows:
//   - Welcome with the user's email
//   - Tier chip (loaded from /api/users/me)
//   - "What's wired up" status grid — the same intelligent-data motif
//     from the hero, but now tracking the user's onboarding progress.
//     Most rows are "coming soon" until weeks 2–10 ship.
//   - Sign out button at the bottom
//
// This screen is intentionally a stub — there's no real product to
// show until the routing engine and map are built. But it does the
// honest thing: confirms auth worked, shows tier, and previews what's
// next without faking features that don't exist.

import { useEffect, useState } from "react";
import { signOut } from "firebase/auth";
import { auth } from "./firebase";
import { apiFetch } from "./api";
import HeroPanel from "./HeroPanel.jsx";

export default function AppView({ user }) {
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/users/me")
      .then((p) => {
        if (!cancelled) setProfile(p);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message || "Could not load profile.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const tier = profile?.tier ?? (loading ? "…" : "unknown");
  const displayName = user.displayName || user.email?.split("@")[0] || "sailor";

  return (
    <div style={styles.shell}>
      <HeroPanel />

      <section style={styles.panel}>
        <div style={styles.inner}>
          <header style={styles.header}>
            <p className="t-label" style={{ marginBottom: 12 }}>
              Signed in
            </p>
            <h1 className="t-display" style={styles.title}>
              Welcome aboard,
              <br />
              <span style={{ color: "var(--ink-3)" }}>{displayName}.</span>
            </h1>

            <div style={styles.tierRow}>
              <TierChip tier={tier} />
              <span className="t-mono" style={styles.email}>
                {user.email}
              </span>
            </div>
          </header>

          {error && (
            <div role="alert" style={styles.error}>
              {error}
            </div>
          )}

          <div style={styles.statusBlock}>
            <p className="t-label" style={{ marginBottom: 16 }}>
              Your setup
            </p>
            <ul style={styles.statusList}>
              <StatusRow status="done" title="Account" detail="Authentication wired up" />
              <StatusRow status="next" title="Boat profile" detail="Pick your class — coming soon" />
              <StatusRow status="next" title="Home waters" detail="Set your race region" />
              <StatusRow
                status="locked"
                title="Pre-race routing"
                detail="Available at launch · Free tier"
              />
              <StatusRow
                status="locked"
                title="In-race routing"
                detail="Pro tier · $15/mo when ready"
              />
            </ul>
          </div>

          <footer style={styles.footer}>
            <button onClick={() => signOut(auth)} style={styles.signOut}>
              Sign out
            </button>
            <span className="t-label">v1.0 · Beta · Great Lakes</span>
          </footer>
        </div>
      </section>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* TierChip — small pill in the right tone for the tier                */
/* ─────────────────────────────────────────────────────────────────── */

function TierChip({ tier }) {
  const styleMap = {
    free: { bg: "var(--paper-2)", color: "var(--ink-2)", border: "var(--rule)" },
    pro: { bg: "var(--ink)", color: "var(--paper)", border: "var(--ink)" },
    hardware: { bg: "var(--accent-soft)", color: "var(--accent)", border: "var(--accent)" },
  };
  const t = styleMap[tier] || styleMap.free;

  return (
    <span
      className="t-mono"
      style={{
        display: "inline-flex",
        alignItems: "center",
        height: 26,
        padding: "0 12px",
        borderRadius: 13,
        background: t.bg,
        color: t.color,
        border: `1px solid ${t.border}`,
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.14em",
        textTransform: "uppercase",
      }}
    >
      {tier}
    </span>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* StatusRow — done / next / locked                                    */
/* ─────────────────────────────────────────────────────────────────── */

function StatusRow({ status, title, detail }) {
  const marks = {
    done: { glyph: "✓", color: "var(--success)", bg: "rgba(45, 143, 91, 0.1)" },
    next: { glyph: "→", color: "var(--accent)", bg: "var(--accent-soft)" },
    locked: { glyph: "·", color: "var(--ink-4)", bg: "var(--paper-2)" },
  };
  const m = marks[status];

  return (
    <li style={styles.statusRow}>
      <span
        style={{
          ...styles.statusMark,
          color: m.color,
          background: m.bg,
        }}
      >
        {m.glyph}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={styles.statusTitle}>{title}</div>
        <div style={styles.statusDetail}>{detail}</div>
      </div>
    </li>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Styles                                                              */
/* ─────────────────────────────────────────────────────────────────── */

const styles = {
  shell: {
    display: "grid",
    gridTemplateColumns: "minmax(0, 1.3fr) minmax(0, 1fr)",
    minHeight: "100vh",
  },
  panel: {
    background: "var(--paper)",
    display: "flex",
    flexDirection: "column",
    padding: "48px 56px",
    minHeight: "100vh",
  },
  inner: {
    width: "100%",
    maxWidth: 460,
    margin: "0 auto",
    flex: 1,
    display: "flex",
    flexDirection: "column",
  },
  header: {
    paddingTop: 24,
    marginBottom: 36,
  },
  title: {
    fontSize: 40,
    margin: "0 0 24px",
  },
  tierRow: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    flexWrap: "wrap",
  },
  email: {
    fontSize: 13,
    color: "var(--ink-3)",
  },
  error: {
    padding: "10px 14px",
    borderRadius: "var(--r-sm)",
    background: "rgba(214, 59, 31, 0.08)",
    color: "var(--error)",
    fontSize: 13,
    border: "1px solid rgba(214, 59, 31, 0.2)",
    marginBottom: 24,
  },
  statusBlock: {
    paddingTop: 32,
    borderTop: "1px solid var(--hair)",
    flex: 1,
  },
  statusList: {
    listStyle: "none",
    margin: 0,
    padding: 0,
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  statusRow: {
    display: "flex",
    alignItems: "flex-start",
    gap: 14,
    padding: "12px 0",
    borderBottom: "1px solid var(--hair)",
  },
  statusMark: {
    width: 24,
    height: 24,
    borderRadius: 6,
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 13,
    fontWeight: 600,
    flexShrink: 0,
    marginTop: 2,
  },
  statusTitle: {
    fontSize: 15,
    fontWeight: 500,
    color: "var(--ink)",
  },
  statusDetail: {
    fontSize: 13,
    color: "var(--ink-3)",
    marginTop: 2,
  },
  footer: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    paddingTop: 32,
  },
  signOut: {
    background: "none",
    border: "1.5px solid var(--rule)",
    height: 38,
    padding: "0 18px",
    borderRadius: "var(--r-md)",
    fontSize: 13,
    fontWeight: 500,
    color: "var(--ink-2)",
    transition: "background 0.15s, border-color 0.15s",
  },
};
