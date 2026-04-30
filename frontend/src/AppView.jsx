// AppView — post-login. Map fills the screen; chrome lives in a slide-in
// menu drawer triggered by the hamburger button (top-right).

import { useEffect, useState } from "react";
import { signOut } from "firebase/auth";
import { auth } from "./firebase";
import { apiFetch } from "./api";
import { MapView } from "./components/MapView.jsx";

export default function AppView({ user }) {
  const [profile, setProfile] = useState(null);
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/users/me")
      .then((p) => !cancelled && setProfile(p))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e) => e.key === "Escape" && setMenuOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  return (
    <div style={styles.shell}>
      <MapView />

      <button
        onClick={() => setMenuOpen(true)}
        style={styles.menuButton}
        aria-label="Open menu"
      >
        <span style={styles.hamburgerLine} />
        <span style={styles.hamburgerLine} />
        <span style={styles.hamburgerLine} />
      </button>

      <MenuDrawer
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
        user={user}
        tier={profile?.tier ?? "…"}
      />
    </div>
  );
}

function MenuDrawer({ open, onClose, user, tier }) {
  return (
    <>
      <div
        onClick={onClose}
        style={{
          ...styles.backdrop,
          opacity: open ? 1 : 0,
          pointerEvents: open ? "auto" : "none",
        }}
      />
      <aside
        style={{
          ...styles.drawer,
          transform: open ? "translateX(0)" : "translateX(100%)",
        }}
      >
        <header style={styles.drawerHeader}>
          <p className="t-label" style={{ marginBottom: 10 }}>
            Signed in
          </p>
          <p style={styles.userEmail}>{user.email}</p>
          <TierChip tier={tier} />
        </header>

        <nav style={styles.nav}>
          <MenuItem label="Race setup" hint="Plan a route" disabled />
          <MenuItem label="Boat profile" hint="Coming soon" disabled />
          <MenuItem label="Home waters" hint="Coming soon" disabled />
          <div style={styles.divider} />
          <MenuItem label="Settings" hint="Account & preferences" disabled />
          <MenuItem label="Help & docs" disabled />
        </nav>

        <footer style={styles.drawerFooter}>
          <button onClick={() => signOut(auth)} style={styles.signOut}>
            Sign out
          </button>
          <span className="t-label" style={{ color: "var(--ink-4)" }}>
            v1.0 · Beta
          </span>
        </footer>
      </aside>
    </>
  );
}

function MenuItem({ label, hint, disabled, onClick }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        ...styles.menuItem,
        opacity: disabled ? 0.5 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      <span style={styles.menuItemLabel}>{label}</span>
      {hint && <span style={styles.menuItemHint}>{hint}</span>}
    </button>
  );
}

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
        height: 24,
        padding: "0 10px",
        borderRadius: 12,
        background: t.bg,
        color: t.color,
        border: `1px solid ${t.border}`,
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.14em",
        textTransform: "uppercase",
      }}
    >
      {tier}
    </span>
  );
}

const styles = {
  shell: {
    position: "relative",
    width: "100%",
    height: "100vh",
    overflow: "hidden",
  },
  menuButton: {
    position: "absolute",
    top: 12,
    right: 12,
    zIndex: 5,
    width: 40,
    height: 40,
    padding: 0,
    border: "none",
    borderRadius: 8,
    background: "rgba(255, 255, 255, 0.94)",
    backdropFilter: "blur(8px)",
    boxShadow: "0 1px 3px rgba(0, 0, 0, 0.08)",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: 4,
    cursor: "pointer",
  },
  hamburgerLine: {
    width: 16,
    height: 1.5,
    background: "var(--ink-2)",
    borderRadius: 1,
  },
  backdrop: {
    position: "fixed",
    inset: 0,
    background: "rgba(15, 23, 42, 0.3)",
    transition: "opacity 0.2s",
    zIndex: 10,
  },
  drawer: {
    position: "fixed",
    top: 0,
    right: 0,
    bottom: 0,
    width: 340,
    background: "var(--paper)",
    boxShadow: "-4px 0 24px rgba(0, 0, 0, 0.08)",
    transition: "transform 0.25s ease-out",
    zIndex: 11,
    display: "flex",
    flexDirection: "column",
    padding: "32px 28px",
  },
  drawerHeader: {
    paddingBottom: 24,
    borderBottom: "1px solid var(--hair)",
  },
  userEmail: {
    margin: "0 0 12px",
    fontSize: 14,
    color: "var(--ink)",
    fontWeight: 500,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  nav: {
    flex: 1,
    paddingTop: 16,
    display: "flex",
    flexDirection: "column",
  },
  menuItem: {
    background: "none",
    border: "none",
    padding: "14px 4px",
    textAlign: "left",
    display: "flex",
    flexDirection: "column",
    gap: 2,
    borderRadius: "var(--r-sm)",
    transition: "background 0.15s",
  },
  menuItemLabel: {
    fontSize: 15,
    color: "var(--ink)",
    fontWeight: 500,
  },
  menuItemHint: {
    fontSize: 12,
    color: "var(--ink-4)",
  },
  divider: {
    height: 1,
    background: "var(--hair)",
    margin: "12px 0",
  },
  drawerFooter: {
    paddingTop: 24,
    borderTop: "1px solid var(--hair)",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
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
    cursor: "pointer",
  },
};
