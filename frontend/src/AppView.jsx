// AppView — post-login. Map fills the screen by default; the hamburger
// menu lets users navigate to the races list, which can in turn open the
// editor or load a race onto the map.
//
// "Active race" = the race rendered on the map (course + countdown).
// Persisted to localStorage so it survives reloads, but cleared
// automatically once the race is more than 6h past its start time —
// no point keeping a finished race front-and-center the next morning.
//
// Code splitting:
//   - MapView is eagerly imported. By the time we get here, the AppView
//     chunk has already paid the mapbox-gl cost — adding a Suspense
//     boundary around the always-mounted base layer would just flicker
//     on every login without saving anything.
//   - RacesListView and RaceEditor are lazy-loaded. Both only mount on
//     user navigation (menu drawer / button click), so the common
//     "open the app, look at wind on the map" path doesn't pull editor
//     code. Each chunk is cached after first use, so the fallback
//     effectively never appears more than once per session.

import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { signOut } from "firebase/auth";
import { auth } from "./firebase";
import { apiFetch } from "./api";
import { MapView } from "./components/MapView.jsx";

const RacesListView = lazy(() => import("./RacesListView.jsx"));
const RaceEditor = lazy(() => import("./RaceEditor.jsx"));

const ACTIVE_RACE_KEY = "sailline.activeRaceId";

// A race is "ongoing" until 6 hours past its scheduled start. After that,
// drop it from active state so the next time the app opens it doesn't
// resurrect a stale course. Also short-circuit if the backend has marked
// the session ended (will happen once in-race tracking lands).
const ONGOING_GRACE_MS = 6 * 60 * 60 * 1000;

function isOngoing(race) {
  if (!race) return false;
  if (race.ended_at) return false;
  if (!race.start_at) return true; // not yet scheduled — still "in planning"
  const startMs = new Date(race.start_at).getTime();
  if (Number.isNaN(startMs)) return true;
  return Date.now() < startMs + ONGOING_GRACE_MS;
}

export default function AppView({ user }) {
  const [profile, setProfile] = useState(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeRace, setActiveRace] = useState(null);

  // view shape: { kind: "map" }
  //           | { kind: "races" }
  //           | { kind: "editor", raceId: string | null, returnTo: "map" | "races" }
  const [view, setView] = useState({ kind: "map" });

  // ── Profile ──────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/users/me")
      .then((p) => !cancelled && setProfile(p))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // ── Restore active race from localStorage on first mount ─────────
  // Strict-mode double-mount guard via ref. Drop the persisted ID if the
  // race is past its window OR was deleted server-side OR fails to load.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    restoredRef.current = true;

    let cancelled = false;
    let id;
    try {
      id = localStorage.getItem(ACTIVE_RACE_KEY);
    } catch {
      return;
    }
    if (!id) return;

    apiFetch(`/api/races/${id}`)
      .then((race) => {
        if (cancelled) return;
        if (isOngoing(race)) {
          setActiveRace(race);
        } else {
          try {
            localStorage.removeItem(ACTIVE_RACE_KEY);
          } catch {
            /* ignore */
          }
        }
      })
      .catch(() => {
        try {
          localStorage.removeItem(ACTIVE_RACE_KEY);
        } catch {
          /* ignore */
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  // ── Menu ESC ─────────────────────────────────────────────────────
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e) => e.key === "Escape" && setMenuOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  // ── Active-race helpers ──────────────────────────────────────────
  const setActive = (race) => {
    setActiveRace(race);
    try {
      if (race) localStorage.setItem(ACTIVE_RACE_KEY, race.id);
      else localStorage.removeItem(ACTIVE_RACE_KEY);
    } catch {
      /* localStorage disabled */
    }
  };

  const goto = (next) => {
    setMenuOpen(false);
    setView(next);
  };

  return (
    <div style={styles.shell}>
      {/* Map is always mounted so it doesn't reinitialize when switching
          back from another view. The other screens render on top of it. */}
      <div style={{ ...styles.layer, zIndex: 0 }}>
        <MapView
          activeRace={activeRace}
          onEditActive={() =>
            activeRace &&
            setView({
              kind: "editor",
              raceId: activeRace.id,
              returnTo: "map",
            })
          }
          onClearActive={() => setActive(null)}
        />
      </div>

      {view.kind === "races" && (
        <div style={{ ...styles.layer, zIndex: 1 }}>
          <Suspense fallback={<ViewLoading />}>
            <RacesListView
              onBack={() => setView({ kind: "map" })}
              onCreate={() =>
                setView({ kind: "editor", raceId: null, returnTo: "races" })
              }
              onOpen={(race) => {
                setActive(race);
                setView({ kind: "map" });
              }}
              onEdit={(id) =>
                setView({ kind: "editor", raceId: id, returnTo: "races" })
              }
            />
          </Suspense>
        </div>
      )}

      {view.kind === "editor" && (
        <div style={{ ...styles.layer, zIndex: 2 }}>
          <Suspense fallback={<ViewLoading />}>
            <RaceEditor
              raceId={view.raceId}
              onClose={() => setView({ kind: view.returnTo || "races" })}
              onSaved={(race) => {
                // After save, the just-saved race becomes active and we
                // land on the map. Map = single pane of glass.
                setActive(race);
                setView({ kind: "map" });
              }}
            />
          </Suspense>
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

// Suspense fallback for lazy-loaded RacesListView / RaceEditor. Sits
// inside the layer wrapper so it inherits the right z-index. After the
// first load each chunk is cached and the fallback effectively never
// appears again in the session.
function ViewLoading() {
  return (
    <div style={styles.viewLoading}>
      <span style={styles.viewLoadingText}>Loading…</span>
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
  viewLoading: {
    position: "absolute",
    inset: 0,
    background: "var(--paper)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },
  viewLoadingText: {
    color: "var(--ink-3)",
    fontSize: 14,
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
