// frontend/src/components/PermissionBanner.jsx
//
// Surfaces a high-visibility warning when Location permission has been
// downgraded relative to what the recorder needs. Two states matter:
//
//   "denied"      — The OS-level toggle is off (or prompt). The
//                   recorder will silently capture zero points. This
//                   is a hard failure; the banner offers an "Open
//                   Settings" action where the platform lets us.
//
//   "background"  — Granted only for foreground use. The recorder
//                   will capture fixes until the screen locks, then
//                   stop without notice. On Android this is the most
//                   common silent-failure mode after "Allow all the
//                   time" → "While using" downgrade.
//
// We do NOT show this banner for "ok" or "unknown" status — surfacing
// "we don't know if your permission is fine" creates more confusion
// than it prevents. Older Safari versions report "unsupported"; we'd
// rather miss a downgrade than scare every iPad user.
//
// Visual style follows BetterRouteBanner: same glass card, same drop-
// down animation. We use a warning palette (amber/orange ink) instead
// of the "faster route" celebratory orange.

import { classifyStatus } from "../lib/permissionStatus";

/**
 * @param {object}  props
 * @param {object|null} props.status   Status from usePermissionStatus.
 * @param {boolean} [props.recording]  When false, suppress the banner
 *                                     — it's a recording-time concern.
 * @param {function} [props.onDismiss] Optional dismiss handler; if
 *                                     omitted the dismiss button is
 *                                     hidden. (Parent owns dismissal
 *                                     state because permission can
 *                                     flip again mid-race.)
 */
export function PermissionBanner({ status, recording = true, onDismiss }) {
  if (!recording) return null;
  const classification = classifyStatus(status);
  if (classification === "ok" || classification === "unknown") return null;

  const isDenied = classification === "denied";
  const title = isDenied
    ? "Location is blocked"
    : "Background location is off";
  const detail = isDenied
    ? "Recording can't capture GPS without Location permission."
    : "Recording will pause as soon as the screen locks.";

  return (
    <div
      className="glass-card--dark permission-banner"
      style={styles.banner}
      role="alert"
      aria-live="assertive"
    >
      <div style={styles.iconWrap} aria-hidden>
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none"
             stroke="currentColor" strokeWidth="2.2"
             strokeLinecap="round" strokeLinejoin="round">
          {/* Warning triangle. */}
          <path d="M12 3 2 21h20L12 3z" />
          <path d="M12 10v5" />
          <circle cx="12" cy="18" r="0.6" fill="currentColor" />
        </svg>
      </div>

      <div style={styles.content}>
        <div style={styles.title}>{title}</div>
        <div style={styles.detail}>{detail}</div>
      </div>

      {onDismiss ? (
        <div style={styles.actions}>
          <button
            type="button"
            onClick={onDismiss}
            className="glass-button--dark"
            style={styles.dismissBtn}
            aria-label="Dismiss"
          >
            Dismiss
          </button>
        </div>
      ) : null}
    </div>
  );
}

const styles = {
  banner: {
    position: "fixed",
    top: 16,
    left: "50%",
    zIndex: 1001, // above BetterRouteBanner — permission is more urgent
    display: "flex",
    alignItems: "center",
    gap: 14,
    padding: "12px 14px 12px 16px",
    minWidth: 360,
    maxWidth: "calc(100% - 32px)",
    animation: "slideDownFade 0.32s var(--ease-out-overshoot) both",
    transform: "translateX(-50%)",
  },
  iconWrap: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: 36,
    height: 36,
    borderRadius: "50%",
    background: "rgba(255, 176, 32, 0.20)",
    color: "#ffb020",
    flexShrink: 0,
  },
  content: { flex: 1, minWidth: 0 },
  title: {
    fontSize: 14,
    fontWeight: 600,
    color: "var(--paper-ink)",
    letterSpacing: "-0.005em",
    lineHeight: 1.25,
  },
  detail: {
    fontSize: 12,
    color: "var(--paper-ink-2)",
    marginTop: 2,
  },
  actions: { display: "flex", gap: 8, flexShrink: 0 },
  dismissBtn: {
    padding: "8px 14px",
    fontSize: 13,
    fontWeight: 500,
    cursor: "pointer",
    fontFamily: "inherit",
  },
};
