// frontend/src/components/BetterRouteBanner.jsx
//
// "Better route available" banner. Slides down from the top of the
// viewport when the SSE notifications stream surfaces an alternative.
// Uses dark frosted glass to match the rest of the map overlays.

export function BetterRouteBanner({ alternative, onAccept, onDismiss }) {
  if (!alternative) return null;

  const minsSaved = Math.round(alternative.improvement_minutes);
  const pctImproved = alternative.improvement_pct.toFixed(1);

  return (
    <div
      className="glass-card--dark better-route-banner"
      style={styles.banner}
      role="alert"
      aria-live="polite"
    >
      <div style={styles.iconWrap} aria-hidden>
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none"
             stroke="currentColor" strokeWidth="2.2"
             strokeLinecap="round" strokeLinejoin="round">
          <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
        </svg>
      </div>

      <div style={styles.content}>
        <div style={styles.title}>Faster route available</div>
        <div className="t-mono" style={styles.detail}>
          Save <strong style={styles.bold}>{minsSaved} min</strong>
          <span style={styles.dot}> · </span>
          {pctImproved}% faster
        </div>
      </div>

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
        <button
          type="button"
          onClick={onAccept}
          className="glass-button--primary-light"
          style={styles.acceptBtn}
          aria-label="Use the new route"
        >
          Use new route
        </button>
      </div>
    </div>
  );
}

const styles = {
  banner: {
    position: "fixed",
    top: 16,
    left: "50%",
    zIndex: 1000,
    display: "flex",
    alignItems: "center",
    gap: 14,
    padding: "12px 14px 12px 16px",
    minWidth: 360,
    maxWidth: "calc(100% - 32px)",
    animation: "slideDownFade 0.32s cubic-bezier(0.2, 0.9, 0.3, 1.15) both",
    transform: "translateX(-50%)",
  },
  // The accent-soft background is rgba orange at 12% - reads as a warm
  // amber tint against dark glass, like a subtle traffic-direction
  // indicator. Keep using --accent for the icon stroke.
  iconWrap: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: 36,
    height: 36,
    borderRadius: "50%",
    background: "rgba(255, 91, 31, 0.20)",
    color: "#ff8a5b",
    flexShrink: 0,
  },
  content: {
    flex: 1,
    minWidth: 0,
  },
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
  bold: {
    color: "var(--paper-ink)",
    fontWeight: 600,
  },
  dot: {
    color: "var(--paper-ink-3)",
  },
  actions: {
    display: "flex",
    gap: 8,
    flexShrink: 0,
  },
  dismissBtn: {
    padding: "8px 14px",
    fontSize: 13,
    fontWeight: 500,
    cursor: "pointer",
    fontFamily: "inherit",
  },
  acceptBtn: {
    padding: "8px 16px",
    fontSize: 13,
    fontWeight: 600,
    cursor: "pointer",
    fontFamily: "inherit",
  },
};
