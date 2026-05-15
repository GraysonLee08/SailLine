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
//   - RacesListView, RaceEditor, ProfileView etc. are lazy-loaded.
//     Each chunk is cached after first use, so the fallback effectively
//     never appears more than once per session.
//   - SensorDebugView is a hidden diagnostic page reachable only via
//     the URL parameter ?debug=sensors. Lazy-loaded so it doesn't
//     bloat the main bundle for normal users.
//
// D4: ``profile_complete`` gates a forced ProfileView. When the
// backend returns ``profile_complete: false`` on /me, we drop the
// user into ProfileView before they can interact with the map. This
// catches first-time email sign-ups (Google sign-ins are auto-
// completed from the ``name`` claim and skip the prompt). The
// accept-invite flow is exempted so a brand-new user can redeem an
// invite link before completing their profile.

import { lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  createTimeline,
  EASE_OUT_SOFT,
  prefersReducedMotion,
  isHidden,
} from "./lib/motion";
import { signOut } from "firebase/auth";
import { auth } from "./firebase";
import { apiFetch } from "./api";
import { MapView } from "./components/MapView.jsx";

const RacesListView = lazy(() => import("./RacesListView.jsx"));
const RaceEditor = lazy(() => import("./RaceEditor.jsx"));
const RaceStatsView = lazy(() => import("./RaceStatsView.jsx"));
const BoatsView = lazy(() => import("./BoatsView.jsx"));
const BoatEditor = lazy(() => import("./BoatEditor.jsx"));
const AcceptInviteView = lazy(() => import("./AcceptInviteView.jsx"));
const ProfileView = lazy(() => import("./ProfileView.jsx"));
const SensorDebugView = lazy(() => import("./SensorDebugView.jsx"));

const ACTIVE_RACE_KEY = "sailline.activeRaceId";
const INTRO_PLAYED_KEY = "sailline.introPlayed";

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
  // Hidden diagnostic page — reachable only via ?debug=sensors. No UI
  // entry point. Short-circuits before any hooks fire so it doesn't
  // pull in profile fetching, active-race restoration, or the map.
  // Safe re: hook-order rules because the URL doesn't change during a
  // single mount, so the early return is either always-taken or never-
  // taken for any given instance of this component.
  if (
    typeof window !== "undefined" &&
    new URLSearchParams(window.location.search).get("debug") === "sensors"
  ) {
    return (
      <Suspense
        fallback={
          <div style={{ height: "100vh", background: "var(--paper)" }} />
        }
      >
        <SensorDebugView />
      </Suspense>
    );
  }

  const [profile, setProfile] = useState(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeRace, setActiveRace] = useState(null);

  // view shape: { kind: "map" }
  //           | { kind: "races" }
  //           | { kind: "editor", raceId: string | null, returnTo: "map" | "races" }
  //           | { kind: "stats", raceId: string, returnTo: "map" | "races" }
  //           | { kind: "boats" }
  //           | { kind: "boat-editor", boatId: string | null, returnTo: "boats" }
  //           | { kind: "accept-invite", code: string }
  //           | { kind: "profile", forced: boolean, returnTo: "map" }
  const [view, setView] = useState(() => {
    if (typeof window === "undefined") return { kind: "map" };
    const code = new URLSearchParams(window.location.search).get("invite");
    return code ? { kind: "accept-invite", code } : { kind: "map" };
  });

  // ── Profile ──────────────────────────────────────────────────────
  // D4: if profile_complete is false AND we're not in the middle of
  // an invite redemption, force the user into ProfileView. We don't
  // route around accept-invite so a brand-new sign-up from an
  // invitation can still join the boat before completing their
  // profile — the forced view will appear next time they open the
  // app (or by accepting we have an opportunity to show the name
  // they got picked from the invite, future enhancement).
  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/users/me")
      .then((p) => {
        if (cancelled) return;
        setProfile(p);
        if (p && p.profile_complete === false && view.kind !== "accept-invite") {
          setView({ kind: "profile", forced: true, returnTo: "map" });
        }
      })
      .catch(() => { });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  // ── Intro timeline (once per session) ────────────────────────────
  // Fades the map subtree from 0 → 1 over 500ms when AppView first mounts.
  // Gated by sessionStorage so reloads / view-switches don't replay.
  // prefers-reduced-motion / hidden-tab → skip and snap to opacity 1.
  const introContainerRef = useRef(null);
  useEffect(() => {
    if (sessionStorage.getItem(INTRO_PLAYED_KEY) === "1") return;

    const root = introContainerRef.current;
    const barbs = root?.querySelector("[data-intro='barbs']");

    // Skip animation under reduced-motion / hidden-tab. Snap target to
    // its final opacity so a subsequent intro never starts mid-frame.
    if (prefersReducedMotion() || isHidden() || !barbs) {
      if (barbs) barbs.style.opacity = "1";
      sessionStorage.setItem(INTRO_PLAYED_KEY, "1");
      return;
    }

    const tl = createTimeline({ defaults: { easing: EASE_OUT_SOFT } });
    tl.add(barbs, { opacity: [0, 1], duration: 500 }, 0);

    sessionStorage.setItem(INTRO_PLAYED_KEY, "1");
    return () => tl.pause?.();
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

  // Display name we show in the menu drawer header. Falls back through
  // display_name → token email → uid so it's never blank.
  const displayLabel =
    profile?.display_name || user?.email || profile?.email || user?.uid || "";

  return (
    <div ref={introContainerRef} style={styles.shell}>
      {/* Map is always mounted so it doesn't reinitialize when switching
          back from another view. The other screens render on top of it. */}
      <div data-intro="barbs" style={{ ...styles.layer, zIndex: 0 }}>
        <MapView
          activeRace={activeRace}
          onRaceCompleted={(raceId) => {
            if (raceId) setView({ kind: "stats", raceId, returnTo: "map" });
          }}
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
              currentUid={user?.uid}
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
              onViewStats={(id) =>
                setView({ kind: "stats", raceId: id, returnTo: "races" })
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
              currentUid={user?.uid}
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

      {view.kind === "stats" && (
        <div style={{ ...styles.layer, zIndex: 2 }}>
          <Suspense fallback={<ViewLoading />}>
            <RaceStatsView
              raceId={view.raceId}
              tier={profile?.tier ?? "free"}
              onBack={() => setView({ kind: view.returnTo || "races" })}
            />
          </Suspense>
        </div>
      )}

      {view.kind === "boats" && (
        <div style={{ ...styles.layer, zIndex: 1 }}>
          <Suspense fallback={<ViewLoading />}>
            <BoatsView
              onBack={() => setView({ kind: "map" })}
              onCreate={() =>
                setView({ kind: "boat-editor", boatId: null, returnTo: "boats" })
              }
              onEdit={(id) =>
                setView({ kind: "boat-editor", boatId: id, returnTo: "boats" })
              }
            />
          </Suspense>
        </div>
      )}

      {view.kind === "boat-editor" && (
        <div style={{ ...styles.layer, zIndex: 2 }}>
          <Suspense fallback={<ViewLoading />}>
            <BoatEditor
              boatId={view.boatId}
              currentUid={user?.uid}
              onClose={() => setView({ kind: view.returnTo || "boats" })}
              onSaved={() => setView({ kind: view.returnTo || "boats" })}
            />
          </Suspense>
        </div>
      )}

      {view.kind === "accept-invite" && (
        <div style={{ ...styles.layer, zIndex: 3 }}>
          <Suspense fallback={<ViewLoading />}>
            <AcceptInviteView
              code={view.code}
              onAccepted={() => {
                // Strip the ?invite= param so a refresh doesn't try
                // to re-accept, then go to Boats so the newly-added
                // boat is visible.
                try {
                  const url = new URL(window.location.href);
                  url.searchParams.delete("invite");
                  window.history.replaceState({}, "", url.toString());
                } catch {
                  /* old browser */
                }
                setView({ kind: "boats" });
              }}
              onCancel={() => {
                try {
                  const url = new URL(window.location.href);
                  url.searchParams.delete("invite");
                  window.history.replaceState({}, "", url.toString());
                } catch {
                  /* ignore */
                }
                setView({ kind: "map" });
              }}
            />
          </Suspense>
        </div>
      )}

      {view.kind === "profile" && (
        <div style={{ ...styles.layer, zIndex: 3 }}>
          <Suspense fallback={<ViewLoading />}>
            <ProfileView
              profile={profile}
              forced={!!view.forced}
              onSaved={(updated) => {
                setProfile(updated);
                // After a successful save we hand the user back to
                // whichever view sent them here (default: map). If
                // they were *forced* in, they only escape once their
                // profile is complete — defensive double-check
                // mirroring the backend so a buggy response can't
                // strand them.
                if (view.forced && !updated.profile_complete) return;
                setView({ kind: view.returnTo || "map" });
              }}
              onCancel={
                view.forced
                  ? undefined
                  : () => setView({ kind: view.returnTo || "map" })
              }
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
        label={displayLabel}
        avatarUrl={profile?.avatar_url}
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

function MenuDrawer({ open, onClose, label, avatarUrl, tier, onNavigate }) {
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
          <div style={styles.drawerIdentity}>
            {avatarUrl ? (
              <img src={avatarUrl} alt="" style={styles.drawerAvatar} />
            ) : (
              <div style={styles.drawerAvatarFallback}>
                {(label || "?").trim().charAt(0).toUpperCase()}
              </div>
            )}
            <p style={styles.drawerEmail}>{label}</p>
          </div>
          <span style={styles.tierChip}>{tier}</span>
        </div>

        <nav style={styles.nav}>
          <NavItem onClick={() => onNavigate({ kind: "races" })}>
            Race setup
          </NavItem>
          <NavItem onClick={() => onNavigate({ kind: "boats" })}>
            Boats
          </NavItem>
          <NavItem disabled>Home waters</NavItem>
          <NavItem
            onClick={() =>
              onNavigate({ kind: "profile", forced: false, returnTo: "map" })
            }
          >
            Settings
          </NavItem>
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
  drawerIdentity: {
    display: "flex",
    alignItems: "center",
    gap: 10,
  },
  drawerAvatar: {
    width: 36,
    height: 36,
    borderRadius: "50%",
    objectFit: "cover",
    flexShrink: 0,
  },
  drawerAvatarFallback: {
    width: 36,
    height: 36,
    borderRadius: "50%",
    background: "#16161a",
    color: "white",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 15,
    fontWeight: 600,
    flexShrink: 0,
  },
  drawerEmail: {
    margin: 0,
    fontSize: 14,
    color: "var(--ink)",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    flex: 1,
    minWidth: 0,
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
