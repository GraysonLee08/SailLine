# Frontend Motion Design — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Layer purposeful motion across the frontend — CSS for everyday transitions, anime.js for hero/analytical moments — and add geolocation follow-mode using Mapbox's native camera animations, all without bloating the auth-screen bundle.

**Architecture:** New `motion.js` library wraps anime.js v4 with reduced-motion / hidden-tab guards. New `useFollowMode` hook holds geolocation follow state and persists per `raceId`. Animations are scoped to components and lazy-loaded via `AppView`. Mapbox camera moves stay on Mapbox's own animation loop.

**Tech Stack:** React 18, Vite, Mapbox GL v3, anime.js v4 (new dep), `@microsoft/fetch-event-source` (existing).

**Spec:** `docs/superpowers/specs/2026-05-06-frontend-motion-design.md`

**TDD note:** The frontend has no test runner today (per spec, "no `lint`/`test` scripts… not required for ship-1"). Strict red-green-refactor isn't viable. Each task has a **manual verification** step in its place — run the dev server, exercise the surface, confirm the described behavior. If a frontend test runner is added later, `useFollowMode` is the most worthwhile candidate to backfill.

**Working directory for all commands:** `frontend/` unless stated otherwise.

---

## Task 1: Install anime.js, add motion tokens, refactor existing keyframes

**Files:**
- Modify: `frontend/package.json` (via `npm install`)
- Modify: `frontend/src/index.css:5-45` (add tokens to `:root`)
- Modify: `frontend/src/glass.css:203-212` (refactor `slideDownFade`, add `pulseRing`)

- [ ] **Step 1: Install anime.js v4**

```powershell
npm install animejs@^4.0.0
```

Expected: `animejs` appears in `package.json` `dependencies`. Confirm version is `4.x`.

- [ ] **Step 2: Add motion tokens to `frontend/src/index.css` `:root` block**

Append these lines inside the `:root { ... }` block (after the existing `--mono` line at line 44, before the closing `}`):

```css
  /* Motion tokens. Mirrored as JS constants in src/lib/motion.js so
     anime.js timelines stay in lockstep with CSS. */
  --motion-fast: 150ms;
  --motion-medium: 320ms;
  --motion-slow: 600ms;
  --motion-hero: 900ms;
  --ease-out-soft: cubic-bezier(0.2, 0.9, 0.3, 1.0);
  --ease-out-overshoot: cubic-bezier(0.2, 0.9, 0.3, 1.15);
```

- [ ] **Step 3: Refactor `slideDownFade` in `frontend/src/glass.css` to use the new tokens, and add `pulseRing`**

Replace the existing `Animations` section (around lines 199-212) with:

```css
/* ────────────────────────────────────────────────────────────────── */
/* Animations                                                          */
/* ────────────────────────────────────────────────────────────────── */

@keyframes slideDownFade {
  from {
    opacity: 0;
    transform: translate(-50%, -12px);
  }
  to {
    opacity: 1;
    transform: translate(-50%, 0);
  }
}

/* Soft accent ring pulsing outward — used by the isochrone "thinking"
   indicator while /api/routing/compute is in flight. CSS keyframe is
   the right tool here (indefinite loop); anime.js would be overkill. */
@keyframes pulseRing {
  0% {
    transform: scale(1);
    opacity: 0.6;
  }
  100% {
    transform: scale(1.6);
    opacity: 0;
  }
}

.pulse-ring {
  position: absolute;
  inset: -8px;
  border-radius: 50%;
  border: 2px solid var(--accent);
  pointer-events: none;
  animation: pulseRing 1.4s var(--ease-out-soft) infinite;
}
```

- [ ] **Step 4: Verify build works**

```powershell
npm run build
```

Expected: Build succeeds. `dist/` is produced. No errors mentioning the new CSS tokens.

- [ ] **Step 5: Commit**

```powershell
git add package.json package-lock.json src/index.css src/glass.css
git commit -m "feat(motion): add anime.js, motion tokens, pulseRing keyframe"
```

---

## Task 2: Create the `motion.js` library

**Files:**
- Create: `frontend/src/lib/motion.js`

- [ ] **Step 1: Write `frontend/src/lib/motion.js`**

```js
// frontend/src/lib/motion.js
//
// Single import surface for anime.js v4. Centralizes shared durations
// and easings so motion stays consistent across components. Wraps
// anime.js's animate() with safety guards so individual components
// don't have to remember to check prefers-reduced-motion or visibility.
//
// Lazy-loaded behind AppView's lazy() boundary in App.jsx — the auth
// screen bundle never pulls anime.js. Verify with `npm run build` and
// confirm the auth-screen chunk has no `animejs` references.

import { animate, createTimeline, stagger } from "animejs";

export { animate, createTimeline, stagger };

// Mirror of CSS tokens in src/index.css. Numeric (ms) for anime.js;
// the CSS tokens are strings for transitions.
export const MOTION_FAST = 150;
export const MOTION_MEDIUM = 320;
export const MOTION_SLOW = 600;
export const MOTION_HERO = 900;

// anime.js v4 accepts standard CSS easing strings.
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
 * Wrap anime.js's `animate()` with the standard guards. If reduced
 * motion is requested or the tab is hidden, snap to the end state
 * (by applying `to` values immediately when `to` is an object of
 * static values) and return null so callers can null-check before
 * calling .pause()/.cancel().
 *
 * Use this instead of importing `animate` directly so the guards
 * can't be forgotten.
 *
 * @param {Element|Element[]|string} target - DOM target(s) for anime
 * @param {object} opts - anime.js v4 options (duration, easing, properties)
 * @returns {object|null} - the anime.js controller, or null if guarded
 */
export function safeAnimate(target, opts) {
  if (prefersReducedMotion() || isHidden()) {
    // Snap to end state. anime.js v4 stores property targets directly
    // on opts; for simple { property: value } pairs we can apply them
    // synchronously as inline styles. For numeric tweens (where opts
    // has an onUpdate that writes to React state) the caller is
    // responsible for snapping — they'll typically set state to the
    // target value when this function returns null.
    snapToEnd(target, opts);
    return null;
  }
  return animate(target, opts);
}

function snapToEnd(target, opts) {
  if (!target || !opts) return;
  const elements = resolveTargets(target);
  for (const el of elements) {
    for (const [key, value] of Object.entries(opts)) {
      if (isAnimeOption(key)) continue;
      const final = Array.isArray(value) ? value[value.length - 1] : value;
      if (final == null || typeof final === "object") continue;
      try {
        el.style[key] = typeof final === "number" ? `${final}px` : String(final);
      } catch {
        // Some style keys (transform, opacity) are fine; ignore others.
      }
    }
  }
}

function resolveTargets(target) {
  if (typeof target === "string") {
    return Array.from(document.querySelectorAll(target));
  }
  if (Array.isArray(target) || target instanceof NodeList) {
    return Array.from(target);
  }
  return [target];
}

const ANIME_OPTION_KEYS = new Set([
  "duration", "delay", "easing", "loop", "direction",
  "autoplay", "begin", "update", "complete", "onUpdate",
  "onComplete", "onBegin", "endDelay",
]);

function isAnimeOption(key) {
  return ANIME_OPTION_KEYS.has(key);
}
```

- [ ] **Step 2: Verify the module imports cleanly**

```powershell
npm run build
```

Expected: Build succeeds. No errors importing `animejs`. The new `lib/motion.js` is included in a chunk.

- [ ] **Step 3: Commit**

```powershell
git add src/lib/motion.js
git commit -m "feat(motion): add motion.js with anime.js wrapper and safety guards"
```

---

## Task 3: Mark drop animation in RaceEditor

**Files:**
- Modify: `frontend/src/RaceEditor.jsx` (mark list render around line 496)

- [ ] **Step 1: Find the mark list render**

```powershell
# From repo root, not frontend/
```
Read `frontend/src/RaceEditor.jsx` around lines 490-540. Confirm the structure: `marks.map((m, i) => <SomeMarkRow key={...} ... />)`.

- [ ] **Step 2: Track the most-recently-added mark id**

In `RaceEditor.jsx`, near the existing `useState`/`useRef` declarations (top of the component, around lines 60-95), add:

```jsx
const [justAddedMarkKey, setJustAddedMarkKey] = useState(null);
```

Then, in EVERY code path that adds a mark (lines 163, 217, 258, 266, 282, 291 — search for `setMarks((prev) => [...prev,` or similar add patterns), capture the new mark's identity. The simplest universal handle: use the array length at insertion time as a key if the codebase doesn't have per-mark stable ids. Inspect the existing add-mark code first; if marks have an `id` field, use that. Otherwise add one (e.g., `crypto.randomUUID()`) at insertion.

Example transformation for one add path:
```jsx
// Before:
setMarks((prev) => [...prev, newMark]);

// After:
const markWithKey = { ...newMark, _animKey: crypto.randomUUID() };
setMarks((prev) => [...prev, markWithKey]);
setJustAddedMarkKey(markWithKey._animKey);
```

If marks are persisted to backend, ensure `_animKey` is a frontend-only attribute that's stripped before save (`marks.map(({_animKey, ...rest}) => rest)`). Check the save path around line 344 (`marks: marks.map((m) => ({...}))`) — add the strip there.

- [ ] **Step 3: Animate the just-added mark on render**

Find the row render (around line 496):

```jsx
{marks.map((m, i) => (
  <SomeMarkRow key={m._animKey || i} ... />
))}
```

Inside the mark row component (or where the row is rendered inline), add an effect:

```jsx
import { useEffect, useRef } from "react";
import { safeAnimate, MOTION_MEDIUM, EASE_OUT_OVERSHOOT } from "./lib/motion";

// inside the row component:
const rowRef = useRef(null);
useEffect(() => {
  if (m._animKey !== justAddedMarkKey) return;
  if (!rowRef.current) return;
  safeAnimate(rowRef.current, {
    scale: [0.4, 1.0],
    opacity: [0, 1],
    duration: 250,  // matches spec §5
    easing: EASE_OUT_OVERSHOOT,
  });
}, [m._animKey, justAddedMarkKey]);

return <div ref={rowRef} ...>{/* ... */}</div>;
```

Pass `justAddedMarkKey` down as a prop if the row is a separate component.

- [ ] **Step 4: Manual verification**

```powershell
npm run dev
```

Open http://localhost:5173, sign in, open the race editor, click the map to drop a mark. The new mark row should scale-bounce in over ~250ms. Existing marks should NOT re-animate.

Test reduced-motion: in Chrome DevTools, open Rendering panel, set "Emulate CSS media feature prefers-reduced-motion" to "reduce". Drop a mark — it should appear instantly (no scale animation).

- [ ] **Step 5: Commit**

```powershell
git add src/RaceEditor.jsx
git commit -m "feat(motion): scale-bounce animation when marks are added"
```

---

## Task 4: BetterRouteBanner count-ups

**Files:**
- Modify: `frontend/src/components/BetterRouteBanner.jsx`

- [ ] **Step 1: Replace static numbers with state-backed values that anime.js tweens**

Update `frontend/src/components/BetterRouteBanner.jsx` to:

```jsx
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

    // Snap immediately if reduced motion / hidden tab — safeAnimate
    // returns null in those cases. We always want the final values
    // visible regardless of animation.
    setAnimMins(targetMins);
    setAnimPct(targetPct);

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
```

Note the inline `animation` style now references `var(--ease-out-overshoot)` instead of the hardcoded cubic-bezier — keeps motion tokens in one place.

- [ ] **Step 2: Manual verification**

To trigger the banner without waiting for a real worker run, temporarily add a debug button in dev:

In Chrome DevTools console with the app open and a race active:
```js
// Force the SSE handler to render an alternative payload by injecting one.
// Easiest: hot-modify useRouteNotifications return in React DevTools, or
// trigger the worker manually:
// From repo root:
//   docker exec or local python -m workers.route_recompute --race-id <id> --dry-run
// (depends on local setup)
```

If easier: temporarily hardcode a fake `alternative` in `MapView.jsx:422` to verify the visual:
```jsx
<BetterRouteBanner
  alternative={{ improvement_minutes: 8.7, improvement_pct: 12.4 }}
  onAccept={() => {}}
  onDismiss={() => {}}
/>
```
Confirm: numbers tick up from 0 to 9 / 12.4 over ~600ms. Revert the hardcode before committing.

Test reduced-motion: same DevTools toggle as Task 3. Numbers should snap to final values immediately.

- [ ] **Step 3: Commit**

```powershell
git add src/components/BetterRouteBanner.jsx
git commit -m "feat(motion): count-up tweens for better-route banner numbers"
```

---

## Task 5: Isochrone "thinking" pulse in RouteControls

**Files:**
- Modify: `frontend/src/components/RouteControls.jsx`

- [ ] **Step 1: Add pulse markup gated on routing.loading**

Read `frontend/src/components/RouteControls.jsx` to find where the start mark or "compute route" affordance is rendered. Inspect the existing `routing` prop usage (search for `routing.loading` or similar).

If `routing.loading` (or an equivalent boolean) isn't already in the component, add it where routing state is consumed. Render a sibling `<div>` with the `pulse-ring` class beside the relevant element:

```jsx
{routing.loading && <div className="pulse-ring" aria-hidden="true" />}
```

Position this inside a `position: relative` parent so the absolute-inset `pulse-ring` overlays correctly. The `.pulse-ring` class was added in Task 1, Step 3.

If the pulse should appear at the start mark inside the Mapbox canvas (not in `RouteControls`), the right home is `MapView.jsx` instead — render an HTML overlay positioned via `map.project([lon, lat])` for the first mark. In that case:

```jsx
// in MapView.jsx, add a state-driven pixel position for the start mark:
const [startMarkPx, setStartMarkPx] = useState(null);
useEffect(() => {
  if (!routing.loading || !activeRace?.marks?.length) {
    setStartMarkPx(null);
    return;
  }
  const m = activeRace.marks[0];
  const map = mapRef.current;
  if (!map) return;
  const update = () => setStartMarkPx(map.project([m.lon, m.lat]));
  update();
  map.on("move", update);
  return () => map.off("move", update);
}, [routing.loading, activeRace]);

// then in JSX, sibling to <BetterRouteBanner>:
{startMarkPx && (
  <div
    className="pulse-ring"
    aria-hidden="true"
    style={{
      position: "absolute",
      left: startMarkPx.x - 16,
      top: startMarkPx.y - 16,
      width: 32,
      height: 32,
    }}
  />
)}
```

**Implement in `RouteControls.jsx`** for ship-1 — simpler, sufficient. The MapView overlay variant (pulse at the actual start mark) is described above for reference and would be a worthwhile follow-up if the simpler version feels insufficient in practice. Do **not** implement both.

- [ ] **Step 2: Manual verification**

```powershell
npm run dev
```

Trigger a route compute (open a race, hit the "compute route" affordance). The pulse ring should animate while the request is in flight, then disappear when the route renders. Check that it respects `prefers-reduced-motion` (`index.css:120-128` already short-circuits all CSS animations).

- [ ] **Step 3: Commit**

```powershell
git add src/components/RouteControls.jsx
# OR src/components/MapView.jsx if you went with the overlay option
git commit -m "feat(motion): pulse ring while isochrone routing is computing"
```

---

## Task 6: Countdown digit slide on second roll-over

**Files:**
- Modify: the countdown render site(s). `useCountdown` is consumed in:
  - `frontend/src/RaceEditor.jsx:126`
  - `frontend/src/components/MapView.jsx:471` (inside the inner `ActiveRaceOverlay` or similar)

- [ ] **Step 1: Read both consumption sites and identify the digit DOM**

Open both files at the cited lines. Identify how the countdown is currently rendered — likely a single string like `{countdown.hours}:{countdown.minutes}:{countdown.seconds}` or a formatted output.

- [ ] **Step 2: Wrap the seconds digit (the one that changes most) in a key'd component that animates on change**

Create a small inline helper near each consumption site, OR add to a new module `frontend/src/components/AnimatedDigit.jsx`. Inline-helper version is fine for ship-1; here's the helper:

```jsx
import { useEffect, useRef } from "react";
import { safeAnimate, EASE_OUT_SOFT } from "../lib/motion";

export function AnimatedDigit({ value, className, style }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return;
    safeAnimate(ref.current, {
      translateY: ["-1em", "0em"],
      opacity: [0, 1],
      duration: 200,
      easing: EASE_OUT_SOFT,
    });
  }, [value]);
  return (
    <span ref={ref} className={className} style={{ display: "inline-block", ...style }}>
      {value}
    </span>
  );
}
```

Save as `frontend/src/components/AnimatedDigit.jsx`.

- [ ] **Step 3: Use `AnimatedDigit` for the seconds field in both render sites**

In each of the two countdown render locations, replace the seconds display with `<AnimatedDigit value={countdown.seconds} />`. Leave hours/minutes static — they don't change every second, so animating them adds noise.

Example (adapt to actual rendering):

```jsx
// Before:
<span>{cd.hours}:{cd.minutes}:{cd.seconds}</span>

// After:
<span>{cd.hours}:{cd.minutes}:<AnimatedDigit value={cd.seconds} /></span>
```

- [ ] **Step 4: Manual verification**

```powershell
npm run dev
```

Open a race with a future `start_at` that's at least a minute away. The seconds digit should slide up + fade in on each tick. Hours/minutes don't animate. Test `prefers-reduced-motion` — seconds should snap (no animation).

- [ ] **Step 5: Commit**

```powershell
git add src/components/AnimatedDigit.jsx src/RaceEditor.jsx src/components/MapView.jsx
git commit -m "feat(motion): slide countdown seconds digit on each tick"
```

---

## Task 7: Route line draw-on via Mapbox `line-trim-offset`

**Files:**
- Modify: `frontend/src/components/MapView.jsx:239-249` (route source/layer setup)
- Modify: `frontend/src/components/MapView.jsx:404` area (where route source data is set on `useRouting` resolve)

- [ ] **Step 1: Enable `lineMetrics` on the route source and add `line-trim-offset` to the layer paint**

Find the route source/layer block at lines 239-249:

```jsx
map.addSource("route", { type: "geojson", data: emptyLine() });
map.addLayer({
  id: "route-line",
  type: "line",
  source: "route",
  paint: {
    "line-color": "#c026d3",
    "line-width": 3,
    "line-opacity": 0.85,
  },
});
```

Replace with:

```jsx
map.addSource("route", {
  type: "geojson",
  data: emptyLine(),
  lineMetrics: true,  // required for line-progress / line-trim-offset
});
map.addLayer({
  id: "route-line",
  type: "line",
  source: "route",
  layout: {
    "line-cap": "round",
    "line-join": "round",
  },
  paint: {
    "line-color": "#c026d3",
    "line-width": 3,
    "line-opacity": 0.85,
    "line-trim-offset": [0, 1],  // start fully trimmed (invisible)
  },
});
```

- [ ] **Step 2: Track the last-drawn route's coordinates and animate `line-trim-offset` on new routes**

Find the route data setter (around line 404, search for `getSource("route")`). Add a ref to track the previously-rendered coordinates and a tween effect:

```jsx
import { safeAnimate, MOTION_SLOW, EASE_OUT_SOFT } from "../lib/motion";

// near other refs in MapView:
const lastRouteCoordsRef = useRef(null);

// in the effect that sets route source data on routing.route change:
useEffect(() => {
  if (!styleLoaded) return;
  const map = mapRef.current;
  const src = map.getSource("route");
  if (!src) return;

  const coords = routing.route?.coordinates ?? null;
  if (!coords || coords.length === 0) {
    src.setData(emptyLine());
    map.setPaintProperty("route-line", "line-trim-offset", [0, 1]);
    lastRouteCoordsRef.current = null;
    return;
  }

  // Set the data first (so the line geometry is in place)
  src.setData({
    type: "Feature",
    geometry: { type: "LineString", coordinates: coords },
    properties: {},
  });

  // Skip the draw-on if the coordinates are identical to the previously
  // rendered route — this is the "from cache" case (per spec §1).
  const same =
    lastRouteCoordsRef.current &&
    coordsEqual(lastRouteCoordsRef.current, coords);
  if (same) {
    map.setPaintProperty("route-line", "line-trim-offset", [0, 0]);
    return;
  }

  // Animate line-trim-offset from [0,1] (invisible) to [0,0] (full).
  const trim = { end: 1.0 };
  map.setPaintProperty("route-line", "line-trim-offset", [0, 1]);

  const ctrl = safeAnimate(trim, {
    end: 0,
    duration: 700,
    easing: EASE_OUT_SOFT,
    onUpdate: () => {
      map.setPaintProperty("route-line", "line-trim-offset", [0, trim.end]);
    },
  });

  // safeAnimate returns null under reduced-motion / hidden — snap to full.
  if (!ctrl) {
    map.setPaintProperty("route-line", "line-trim-offset", [0, 0]);
  }

  lastRouteCoordsRef.current = coords;

  return () => {
    if (ctrl?.pause) ctrl.pause();
  };
}, [routing.route, styleLoaded]);
```

Add the `coordsEqual` helper at the top-level of the file (or import from `lib/`):

```js
function coordsEqual(a, b) {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i][0] !== b[i][0] || a[i][1] !== b[i][1]) return false;
  }
  return true;
}
```

- [ ] **Step 3: Manual verification**

```powershell
npm run dev
```

1. Open a race with a computed route. The line should draw on from start to end over ~700ms.
2. Reload the page. The route is cache-hit — it should appear instantly (no draw-on).
3. Edit the race (move a mark) and recompute. New route should draw on.
4. Toggle `prefers-reduced-motion: reduce` in DevTools and recompute. The route should snap to fully visible immediately.

- [ ] **Step 4: Commit**

```powershell
git add src/components/MapView.jsx
git commit -m "feat(motion): animated route line draw-on via line-trim-offset"
```

---

## Task 8: Auth → app intro timeline

**Files:**
- Modify: `frontend/src/AppView.jsx`

- [ ] **Step 1: Add a one-shot intro effect gated by sessionStorage**

Open `frontend/src/AppView.jsx`. After the existing `useState`/`useEffect` block (the `activeRace` state at line 50 area), add:

```jsx
import { useEffect, useRef } from "react";
import {
  safeAnimate,
  createTimeline,
  MOTION_HERO,
  EASE_OUT_SOFT,
  prefersReducedMotion,
  isHidden,
} from "./lib/motion";

const INTRO_PLAYED_KEY = "sailline.introPlayed";

// inside AppView component, near other effects:
const introContainerRef = useRef(null);

useEffect(() => {
  if (sessionStorage.getItem(INTRO_PLAYED_KEY) === "1") return;
  if (prefersReducedMotion() || isHidden()) {
    sessionStorage.setItem(INTRO_PLAYED_KEY, "1");
    return;
  }
  if (!introContainerRef.current) return;

  // Resolve children by data attribute so the timeline is robust to
  // markup tweaks (no brittle :nth-child selectors).
  const root = introContainerRef.current;
  const sweep = root.querySelector("[data-intro='sweep']");
  const trace = root.querySelector("[data-intro='trace']");
  const barbs = root.querySelector("[data-intro='barbs']");

  const tl = createTimeline({ defaults: { easing: EASE_OUT_SOFT } });
  if (sweep) tl.add(sweep, { opacity: [1, 0], duration: 250 }, 0);
  if (trace) tl.add(trace, { opacity: [0, 1], duration: 500 }, 200);
  if (barbs) tl.add(barbs, { opacity: [0, 1], duration: 500 }, 400);

  sessionStorage.setItem(INTRO_PLAYED_KEY, "1");
  return () => tl.pause?.();
}, []);
```

- [ ] **Step 2: Identify which JSX elements should carry the intro data attributes**

`AppView.jsx` already renders the post-login shell. Decide which existing elements to tag:
- `[data-intro="sweep"]` — A short-lived overlay. If no obvious target exists, add a single `<div>` overlay (`position: fixed`, full-viewport, `var(--paper)` background) inside `<div ref={introContainerRef}>` that's removed after fade-out via the timeline's `onComplete` (or simply left at opacity 0).
- `[data-intro="trace"]` — A faint sample SVG path overlaid on the map. For ship-1, this can be a static SVG `<path>` with low opacity (`stroke-opacity: 0.25`), positioned absolutely inside the map container, that fades from 0 → 1.
- `[data-intro="barbs"]` — The wind barb SVG layer container. If wind barbs render via Mapbox source data (not DOM SVG), then "fading them in" is harder; either skip this animation step, OR fade in the entire MapView wrapper from 0 → 1 instead. Default: tag the MapView outer wrapper as `[data-intro="barbs"]`.

Wrap the relevant subtree:
```jsx
<div ref={introContainerRef} style={{ position: "relative" }}>
  <div data-intro="sweep" style={{
    position: "fixed", inset: 0, background: "var(--paper)",
    pointerEvents: "none", zIndex: 999,
  }} />
  <div data-intro="trace">{/* optional SVG trace, can be omitted for ship-1 */}</div>
  <div data-intro="barbs">
    {/* existing MapView and friends */}
  </div>
</div>
```

If any of the three targets is missing/awkward, omit it from the timeline; the `if (sweep)` guards handle absent elements gracefully.

- [ ] **Step 3: Manual verification**

```powershell
npm run dev
```

1. Sign out, sign in again. The intro timeline should play ~900ms (paper sweep clears, optional trace fades in, map fades in).
2. Reload the page (still signed in). The intro should NOT play again — gated by `sessionStorage`.
3. In DevTools `Application → Session Storage`, clear `sailline.introPlayed`, reload — intro plays again. This is expected.
4. Toggle reduced-motion and reload (clear the key first). Intro should be skipped; map appears immediately.

- [ ] **Step 4: Commit**

```powershell
git add src/AppView.jsx
git commit -m "feat(motion): once-per-session auth-to-app intro timeline"
```

---

## Task 9: `useFollowMode` hook

**Files:**
- Create: `frontend/src/hooks/useFollowMode.js`

- [ ] **Step 1: Write the hook**

```js
// frontend/src/hooks/useFollowMode.js
//
// Geolocation follow-mode state for the active-race map view. Models
// Google-Maps-style "follow me" — the camera tracks the user's
// position until the user manually pans/zooms/rotates, at which point
// follow flips off. A "Re-center" pill button (rendered by MapView)
// lets the user re-engage.
//
// Persisted per-raceId in sessionStorage so a reload mid-race
// preserves intent. The non-null raceId argument IS the "race is
// active" gate — the hook is only ever instantiated with a real id.

import { useCallback, useEffect, useState } from "react";

const KEY = (raceId) => `sailline.follow:${raceId}`;

export function useFollowMode(raceId) {
  const [following, setFollowingState] = useState(() => {
    if (!raceId) return false;
    if (typeof sessionStorage === "undefined") return true;
    const stored = sessionStorage.getItem(KEY(raceId));
    if (stored === "1") return true;
    if (stored === "0") return false;
    return true;  // default for new race
  });

  const setFollowing = useCallback(
    (value) => {
      setFollowingState(value);
      if (raceId && typeof sessionStorage !== "undefined") {
        sessionStorage.setItem(KEY(raceId), value ? "1" : "0");
      }
    },
    [raceId],
  );

  // Reset state when raceId changes — different race, different
  // persisted preference.
  useEffect(() => {
    if (!raceId) {
      setFollowingState(false);
      return;
    }
    if (typeof sessionStorage === "undefined") return;
    const stored = sessionStorage.getItem(KEY(raceId));
    if (stored === "1") setFollowingState(true);
    else if (stored === "0") setFollowingState(false);
    else setFollowingState(true);
  }, [raceId]);

  // recenter() is a higher-level convenience: flips following on AND
  // signals to MapView that a one-shot re-pan should happen. We
  // surface a counter that increments on each call so MapView's
  // effect can react to the bump without coupling to internal state.
  const [recenterTick, setRecenterTick] = useState(0);
  const recenter = useCallback(() => {
    setFollowing(true);
    setRecenterTick((n) => n + 1);
  }, [setFollowing]);

  return { following, setFollowing, recenter, recenterTick };
}
```

- [ ] **Step 2: Manual smoke (no unit test runner available)**

In `MapView.jsx`, temporarily add at the top of the component:
```jsx
import { useFollowMode } from "../hooks/useFollowMode";
const fm = useFollowMode(activeRace?.id ?? null);
console.log("[follow]", fm.following, fm.recenterTick);
```

Sign in, open a race. Console should log `[follow] true 0` initially. In DevTools `Application → Session Storage`, set `sailline.follow:<raceId>` to `"0"`, reload — console should log `[follow] false 0`.

Remove the temporary import + console.log before committing this task. (Task 10 will wire it for real.)

- [ ] **Step 3: Commit**

```powershell
git add src/hooks/useFollowMode.js
git commit -m "feat(motion): useFollowMode hook with sessionStorage persistence"
```

---

## Task 10: Wire follow-mode into MapView (gestures, easeTo, Re-center pill)

**Files:**
- Modify: `frontend/src/components/MapView.jsx`

- [ ] **Step 1: Import the hook and wire it**

In `frontend/src/components/MapView.jsx`, near other hook imports (around line 36-40), add:

```jsx
import { useFollowMode } from "../hooks/useFollowMode";
```

Inside `MapView` component, near the other hook calls (around line 154-156):

```jsx
const followMode = useFollowMode(activeRace?.id ?? null);
```

- [ ] **Step 2: Add gesture handlers that flip follow off on user interaction**

In the same `useEffect` that initializes the map (around line 256, where existing event handlers like `map.on("moveend", pushViewport)` are attached), add:

```jsx
const userGestureHandler = (e) => {
  // Only react to user-initiated events. Mapbox events fired by
  // map.easeTo (programmatic) have no originalEvent.
  if (e.originalEvent) {
    followModeRef.current.setFollowing(false);
  }
};
map.on("dragstart", userGestureHandler);
map.on("zoomstart", userGestureHandler);
map.on("rotatestart", userGestureHandler);
```

Add a ref to read latest `followMode.setFollowing` without re-running the init effect:

```jsx
// near other refs at the top of the component:
const followModeRef = useRef(followMode);
followModeRef.current = followMode;
```

- [ ] **Step 3: Pan the camera when position updates and follow is on**

Add a new effect:

```jsx
// One camera move per second max (debounce against geolocation jitter).
const lastEaseAtRef = useRef(0);

useEffect(() => {
  if (!styleLoaded) return;
  if (!followMode.following) return;
  if (!position?.lat || !position?.lon) return;
  const map = mapRef.current;
  if (!map) return;

  const now = Date.now();
  if (now - lastEaseAtRef.current < 1000) return;
  lastEaseAtRef.current = now;

  map.easeTo({
    center: [position.lon, position.lat],
    duration: 600,
    easing: (t) => 1 - Math.pow(1 - t, 3),  // ease-out cubic
  });
}, [position, followMode.following, styleLoaded]);
```

Note: `position` comes from the existing `useGeolocation` hook (find its existing destructure in MapView and reuse).

- [ ] **Step 4: One-shot recenter on `recenterTick` bump**

```jsx
useEffect(() => {
  if (!styleLoaded) return;
  if (followMode.recenterTick === 0) return;
  if (!position?.lat || !position?.lon) return;
  const map = mapRef.current;
  if (!map) return;
  map.easeTo({
    center: [position.lon, position.lat],
    zoom: Math.max(map.getZoom(), 14),
    duration: 800,
    easing: (t) => 1 - Math.pow(1 - t, 3),
  });
}, [followMode.recenterTick, styleLoaded]);
```

- [ ] **Step 5: Render the Re-center pill button**

Add to the JSX (sibling to `<BetterRouteBanner>`, around line 422):

```jsx
{!followMode.following && position?.lat && (
  <button
    type="button"
    onClick={followMode.recenter}
    className="glass-button--dark"
    aria-label="Re-center on my position"
    style={{
      position: "fixed",
      bottom: 24,
      right: 24,
      zIndex: 1000,
      padding: "10px 14px",
      fontSize: 13,
      fontWeight: 500,
      cursor: "pointer",
      fontFamily: "inherit",
      animation: "slideDownFade 0.18s var(--ease-out-soft) both",
      transform: "none",  // override the slideDownFade -50% translate
    }}
  >
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none"
         stroke="currentColor" strokeWidth="2.2"
         strokeLinecap="round" strokeLinejoin="round"
         style={{ verticalAlign: "-3px", marginRight: 6 }}>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
    </svg>
    Re-center
  </button>
)}
```

The inline `transform: "none"` override is needed because the existing `slideDownFade` keyframe targets the centered banner (`translate(-50%, -12px)`); we want the pill to appear in its natural position. If this looks awkward, use a separate keyframe (`pillFadeIn`) defined in `glass.css`:

```css
@keyframes pillFadeIn {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}
```

and reference it instead.

- [ ] **Step 6: Manual verification**

```powershell
npm run dev
```

1. Sign in, open an active race. The map should pan smoothly to your position as it updates (allow geolocation when prompted; or simulate via Chrome DevTools `Sensors → Location`).
2. Manually drag the map. Follow-mode should flip off. The "Re-center" pill should appear in the bottom-right.
3. Click "Re-center". The map should pan back and zoom in slightly. The pill disappears.
4. Reload the page. If you'd flipped follow-mode off, it should stay off (sessionStorage). If you'd left it on, it stays on.
5. Open a different race. Follow-mode should reset to the default (true) for that race.

Test reduced-motion: panning still happens, but `easeTo` doesn't honor `prefers-reduced-motion` natively — that's acceptable since camera moves aren't decorative. The pill's slideDownFade IS short-circuited by the existing `index.css` rule.

- [ ] **Step 7: Commit**

```powershell
git add src/components/MapView.jsx src/glass.css
git commit -m "feat(motion): geolocation follow-mode with re-center pill"
```

---

## Task 11: Bundle-size verification and final smoke

**Files:** none modified — verification only.

- [ ] **Step 1: Build and inspect chunks**

```powershell
npm run build
```

Then list the produced chunks:
```powershell
ls dist/assets
```

Identify the auth-screen chunk (the smallest non-vendor chunk that loads on the unauthenticated path; in this codebase's lazy() pattern, it's the chunk containing `AuthView`). Open it in an editor and search for `animejs` references. Expected: zero matches. If matches are found, anime.js leaked into the eager bundle — investigate `lib/motion.js` import sites and confirm only `AppView` and its descendants import it.

```powershell
# Quick search using grep tool on dist:
# (Use the Grep tool; this is a verification step, not a code change.)
```

- [ ] **Step 2: Run the full smoke checklist in the dev server**

```powershell
npm run dev
```

Verify each animation in order:
- [ ] Auth → app intro plays once per session (clear `sailline.introPlayed` to retest)
- [ ] Mark drop scale-bounces in RaceEditor
- [ ] Banner count-ups tween when an alternative arrives (force one or hardcode for visual check)
- [ ] Countdown seconds digit slides each second
- [ ] Route line draws on after `useRouting` resolves; cache hit appears instantly
- [ ] Isochrone pulse ring shows during `routing.loading`
- [ ] Geolocation follow pans smoothly, breaks on user gesture, recovers via Re-center pill

Then with `prefers-reduced-motion: reduce` enabled:
- [ ] All count-ups snap to final values
- [ ] Mark drop appears instantly
- [ ] Countdown digit doesn't slide
- [ ] Route line appears fully drawn (no animation)
- [ ] Auth → app intro is skipped
- [ ] Pulse ring still pulses (CSS keyframe is short-circuited by `index.css:120-128`'s rule that sets `animation-duration: 0.01ms` — so it pulses imperceptibly fast)
- [ ] Follow-mode `easeTo` still happens (acceptable — camera moves aren't decorative)

- [ ] **Step 3: No commit needed** (verification-only task)

If any check fails, revisit the relevant task and patch — then re-run the smoke.

---

## After this plan

The full motion design ships in 10 commits. To extend later (per spec § "Open follow-ups"):

- **AI tactician suggestion animations** — when that feature ships, the new components import from `lib/motion.js` and use `safeAnimate`/`createTimeline`.
- **Course-up bearing toggle** — in `useFollowMode`, accept a `bearingMode` arg, pass `bearing: heading` to `easeTo` when course-up.
- **Leg-list auto-scroll** — when a scrollable leg-list UI ships, use `Element.scrollIntoView({ behavior: "smooth" })`; no anime.js needed.
- **Frontend test harness** — when added, the highest-value backfill is `useFollowMode` (state machine logic). Animation behavior remains manual-smoke.
