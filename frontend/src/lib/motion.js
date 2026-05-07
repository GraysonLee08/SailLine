// frontend/src/lib/motion.js
//
// Single import surface for anime.js v4. Centralizes shared durations
// and easings so motion stays consistent across components. Exposes
// safeAnimate(): a thin wrapper around anime.js's animate() that bails
// (returning null) under prefers-reduced-motion or when the tab is
// hidden, so individual components don't have to remember the guards.
//
// IMPORTANT: safeAnimate does NOT snap the target to its end state on
// bail. Callers are responsible for ensuring the desired final visual
// state holds when safeAnimate returns null. Two common patterns:
//   1. Pure entrance animations (scale/opacity from 0 → 1, etc.):
//      the unanimated state IS the final state. A null return means
//      the element just appears immediately. Nothing to do.
//   2. Numeric tweens with onUpdate writing to React state: set state
//      to the FINAL value BEFORE calling safeAnimate. If the animation
//      runs, onUpdate will overwrite intermediate values; if it bails,
//      the final value already holds.
//
// Lazy-loaded behind AppView's lazy() boundary in App.jsx — the auth
// screen bundle never pulls anime.js.

import { animate, createTimeline, stagger } from "animejs";

// createTimeline and stagger are exposed for callers that need them
// (e.g., AppView's intro timeline). They are NOT guarded — callers
// that import them must check prefersReducedMotion()/isHidden()
// themselves before orchestrating.
export { createTimeline, stagger };

// Mirror of CSS tokens in src/index.css. Numeric (ms) for anime.js;
// the CSS tokens are strings for transitions.
export const MOTION_FAST = 150;
export const MOTION_MEDIUM = 320;
export const MOTION_SLOW = 600;
export const MOTION_HERO = 900;

// anime.js v4 accepts CSS-style easing strings.
export const EASE_OUT_SOFT = "cubicBezier(0.2, 0.9, 0.3, 1.0)";
export const EASE_OUT_OVERSHOOT = "cubicBezier(0.2, 0.9, 0.3, 1.15)";

/** True when the user has requested reduced motion. */
export function prefersReducedMotion() {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** True when the document is hidden (background tab, screen lock). */
export function isHidden() {
  if (typeof document === "undefined") return false;
  return document.visibilityState !== "visible";
}

/**
 * Run anime.js animate() unless reduced motion is requested or the
 * tab is hidden, in which case return null without animating.
 *
 * The caller owns the final state — see file header for patterns.
 *
 * @param {Element|Element[]|string|object} target - anime.js target(s)
 * @param {object} opts - anime.js v4 options
 * @returns {object|null} - the anime.js controller, or null if guarded
 */
export function safeAnimate(target, opts) {
  if (prefersReducedMotion() || isHidden()) return null;
  return animate(target, opts);
}
