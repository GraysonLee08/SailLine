// AppView — post-login. Map fills the screen by default; the hamburger
// menu lets users navigate to the races list, which can in turn open the
// editor. View routing is a flat useState because we have three screens
// and no URLs yet — react-router can land later if any screen needs to
// be deep-linkable.

import { useEffect, useState } from "react";
import { signOut } from "firebase/auth";
import { auth } from "./firebase";
import { apiFetch } from "./api";
import { MapView } from "./components/MapView.jsx";
import RacesListView from "./RacesListView.jsx";
import RaceEditor from "./RaceEditor.jsx";

export default function AppView({ user }) {
  const [profile, setProfile] = useState(null);
  const [menuOpen, setMenuOpen] = useState(false);

  // view shape: { kind: "map" }
  //           | { kind: "races" }
  //           | { kind: "editor", raceId: string | null }
  const [view, setView] = useState({ kind: "map" });

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

  const goto = (next) => {
    setMenuOpen(false);
    setView(next);
  };

  return (
    <div style={styles.shell}>
      {/* Map is always mounted so it doesn't reinitialize when switching
          back from another view. The other screens render on top of it. */}
      <div style={{ ...styles.layer, zIndex: 0 }}>
        <MapView />
      </div>

      {view.kind === "races" && (
        <div style={{ ...styles.layer, zIndex: 1 }}>
          <RacesListView
            onBack={() => setView({ kind: "map" })}
            onCreate={() => setView({ kind: "editor", raceId: null })}
            onOpen={(id) => setView({ kind: "editor", raceId: id })}
          />
        </div>
      )}

      {view.kind === "editor" && (
        <div style={{ ...styles.layer, zIndex: 2 }}>
          <RaceEditor
            raceId={view.raceId}
            onClose={() => setView({ kind: "races" })}
            onSaved={() => {
              /* RacesListView refetches via its hook on remount */
            }}
          />
        </div>
      )}

      {/* Hamburger only when the map is the active view — other screens
          have their own back / cancel controls in their headers. */}
      {view.kind === "map" && (
        <button
          onClick={() => setMenuOpen(true)}
          style={styles.menuButton}
          aria-label="Open menu"
        >
          <span style={styles.hamburgerLine} />
          <span style={styles.hamburgerLine} />
          <span style={styles.hamburgerLine} />
        </button>
      )}

      <MenuDrawer
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
        user={user}
        tier={profile?.tier ?? "…"}
        onNavigate={goto}
      />
    </div>
  );
}

function MenuDrawer({ open, onClose, user, tier, onNavigate }) {
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
        <div style={styles.drawerHeader}>
          <p style={styles.drawerEmail}>{user.email || user.uid}</p>
          <span style={styles.tierChip}>{tier}</span>
        </div>

        <nav style={styles.nav}>
          <NavItem onClick={() => onNavigate({ kind: "races" })}>
            Race setup
          </NavItem>
          <NavItem disabled>Boat profile</NavItem>
          <NavItem disabled>Home waters</NavItem>
          <NavItem disabled>Settings</NavItem>
          <NavItem disabled>Help</NavItem>
        </nav>

        <button
          onClick={() => {
            onClose();
            signOut(auth);
          }}
          style={styles.signOutBtn}
        >
          Sign out
        </button>
      </aside>
    </>
  );
}

function NavItem({ onClick, disabled, children }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        ...styles.navItem,
        color: disabled ? "var(--ink-4)" : "var(--ink)",
        cursor: disabled ? "default" : "pointer",
      }}
    >
      {children}
      {!disabled && <span style={styles.navArrow}>→</span>}
    </button>
  );
}

const styles = {
  shell: {
    position: "relative",
    height: "100vh",
    width: "100vw",
    overflow: "hidden",
  },
  layer: {
    position: "absolute",
    inset: 0,
  },
  menuButton: {
    position: "absolute",
    top: 16,
    right: 16,
    width: 44,
    height: 44,
    background: "var(--paper)",
    border: "1px solid var(--rule)",
    borderRadius: "var(--r-sm)",
    cursor: "pointer",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: 4,
    boxShadow: "0 1px 3px rgba(0,0,0,0.1)",
    zIndex: 10,
  },
  hamburgerLine: {
    width: 18,
    height: 1.5,
    background: "var(--ink)",
  },
  backdrop: {
    position: "absolute",
    inset: 0,
    background: "rgba(0,0,0,0.3)",
    transition: "opacity 0.2s",
    zIndex: 20,
  },
  drawer: {
    position: "absolute",
    top: 0,
    right: 0,
    width: 340,
    height: "100%",
    background: "var(--paper)",
    boxShadow: "-2px 0 12px rgba(0,0,0,0.1)",
    transition: "transform 0.25s ease",
    display: "flex",
    flexDirection: "column",
    padding: "32px 28px",
    zIndex: 21,
    boxSizing: "border-box",
  },
  drawerHeader: {
    paddingBottom: 24,
    borderBottom: "1px solid var(--rule)",
    marginBottom: 16,
  },
  drawerEmail: {
    margin: 0,
    fontSize: 14,
    color: "var(--ink)",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  tierChip: {
    display: "inline-block",
    marginTop: 8,
    padding: "2px 10px",
    fontSize: 11,
    textTransform: "uppercase",
    letterSpacing: "0.04em",
    background: "rgba(22,22,26,0.05)",
    color: "var(--ink)",
    borderRadius: 999,
    fontWeight: 500,
  },
  nav: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
  },
  navItem: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    background: "none",
    border: "none",
    padding: "14px 0",
    fontSize: 15,
    textAlign: "left",
    fontFamily: "inherit",
    borderBottom: "1px solid var(--rule)",
  },
  navArrow: {
    color: "var(--ink-4)",
    fontFamily: "var(--mono, monospace)",
    fontSize: 14,
  },
  signOutBtn: {
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    padding: "12px",
    fontSize: 14,
    color: "var(--ink)",
    cursor: "pointer",
    fontFamily: "inherit",
  },
};
