// frontend/src/components/BetterRouteBanner.jsx
//
// "Better route available" banner. Slides down from the top of the
// viewport when the SSE notifications stream surfaces an alternative.
// Numbers (minutes saved, % faster) tween from 0 to their final values
// on entry so the user *sees* the savings register.

import { useEffect, useState } from "react";
import { safeAnimate, MOTION_SLOW, EASE_OUT_SOFT } from "../lib/motion";

export function BetterRouteBanner({ alternative, onAccept, onDismiss }) {
  const [animMins, setAnimMins] = useState(0);
  const [animPct, setAnimPct] = useState(0);

  // Tween 0 -> final values when an alternative payload arrives.
  // We use anime.js's onUpdate to drive React state because the values
  // are formatted for display (rounded mins, fixed-1 pct), not raw
  // CSS properties.
  useEffect(() => {
    if (!alternative) {
      setAnimMins(0);
      setAnimPct(0);
      return;
    }
    const targetMins = alternative.improvement_minutes;
    const targetPct = alternative.improvement_pct;

    const tween = { mins: 0, pct: 0 };
    const ctrl = safeAnimate(tween, {
      mins: targetMins,
      pct: targetPct,
      duration: MOTION_SLOW,
      easing: EASE_OUT_SOFT,
      onUpdate: () => {
        setAnimMins(tween.mins);
        setAnimPct(tween.pct);
      },
    });

    // Reduced-motion / hidden-tab: safeAnimate returned null, so onUpdate
    // will never fire. Snap to the final values explicitly. In the normal
    // path, the tween progresses 0 → target naturally and the initial
    // render's animMins=0 / animPct=0 is the correct starting frame.
    if (!ctrl) {
      setAnimMins(targetMins);
      setAnimPct(targetPct);
    }

    return () => {
      if (ctrl?.pause) ctrl.pause();
    };
  }, [alternative?.improvement_minutes, alternative?.improvement_pct]);

  if (!alternative) return null;

  const minsDisplay = Math.round(animMins);
  const pctDisplay = animPct.toFixed(1);

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
          Save <strong style={styles.bold}>{minsDisplay} min</strong>
          <span style={styles.dot}> · </span>
          {pctDisplay}% faster
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
    background: "rgba(255, 91, 31, 0.20)",
    color: "#ff8a5b",
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
  bold: {
    color: "var(--paper-ink)",
    fontWeight: 600,
  },
  dot: { color: "var(--paper-ink-3)" },
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
