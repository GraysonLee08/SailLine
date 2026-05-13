# Frontend Motion Design — Spec

**Date:** 2026-05-06
**Status:** Approved, pending implementation plan
**Owner:** Grayson VanderLinde

## Goal

Make the frontend feel modern and high-tech in a way that reinforces the app's analytical character — *the system is computing for you* — without compromising performance. The existing design (glassmorphism over a light Mapbox base, restrained accent orange, Inter + JetBrains Mono) is already opinionated; this work is a motion pass that lands on top of it, not a redesign.

The split is **CSS for everyday motion, anime.js for hero / analytical moments**, plus one small new feature (geolocation follow-mode) that uses Mapbox's native camera animations.

## Non-goals

- No redesign of the visual language (colors, typography, glass tokens stay as-is).
- No Three.js, WebGL shader effects, or map-layer custom shaders.
- No page-level route transitions beyond the auth → app boundary (the app is largely single-screen).
- No background-tab geolocation tracking.
- No leg-list auto-scroll feature — there is no scrollable leg list today; deferred until/unless one ships.
- No AI-advisor animations — feature is not shipped yet.

## Architecture

### New files

- **`frontend/src/lib/motion.js`** — single import surface for `animejs`. Exports:
  - Re-exports of `animate`, `createTimeline`, `stagger` from `animejs`.
  - Shared duration constants (`MOTION_FAST = 150`, `MOTION_MEDIUM = 320`, `MOTION_SLOW = 600`, `MOTION_HERO = 900`) and easing strings (`EASE_OUT_SOFT`, `EASE_OUT_OVERSHOOT`) that mirror the CSS tokens.
  - Two guard helpers:
    - `prefersReducedMotion()` — reads the media query.
    - `isHidden()` — `document.visibilityState !== "visible"`.
  - A `safeAnimate(target, opts)` wrapper that checks both guards; on bail it snaps the target to its end state and returns a resolved promise. Components call `safeAnimate` instead of `animate` directly so the guards can't be forgotten.

- **`frontend/src/hooks/useFollowMode.js`** — geolocation follow-mode state. Signature: `useFollowMode(raceId)`.
  - Returns `{ following, recenter, setFollowing }`.
  - `following` initial value: read from `sessionStorage["follow:" + raceId]` if present; otherwise default to `true` when `raceId` is non-null, `false` when null. (The non-null `raceId` IS the "race is active" signal — the hook is only ever instantiated with a real race id.)
  - `setFollowing(bool)` is the low-level setter wired to Mapbox gesture handlers in `MapView` (`dragstart` etc. call `setFollowing(false)`).
  - `recenter()` is a higher-level convenience: calls `setFollowing(true)` and triggers a one-time pan to current position. Bound to the "Re-center" pill button.
  - Every state change is persisted back to `sessionStorage["follow:" + raceId]`.

### Modified files

- **`frontend/src/index.css`** — add motion tokens to `:root`:
  ```css
  --motion-fast: 150ms;
  --motion-medium: 320ms;
  --motion-slow: 600ms;
  --motion-hero: 900ms;
  --ease-out-soft: cubic-bezier(0.2, 0.9, 0.3, 1.0);
  --ease-out-overshoot: cubic-bezier(0.2, 0.9, 0.3, 1.15);
  ```
  Existing `prefers-reduced-motion` media query stays as-is.

- **`frontend/src/glass.css`** — refactor existing `slideDownFade` to reference the new tokens. Add `pulseRing` keyframe for the isochrone "thinking" indicator.

- **`frontend/src/components/MapView.jsx`** — wire `useFollowMode`, attach Mapbox gesture handlers that set `following = false` on user-initiated camera moves, render the "Re-center" pill conditionally.

- **`frontend/src/components/BetterRouteBanner.jsx`** — replace the snap-rendered `improvement_minutes` and `improvement_pct` numbers with anime.js count-ups.

- **`frontend/src/components/RouteControls.jsx`** — add the isochrone "thinking" pulse around the start mark while `useRouting().loading`.

- **`frontend/src/RaceEditor.jsx`** — animate mark drop on add.

- **`frontend/src/AppView.jsx`** — orchestrate the auth → app intro timeline on mount.

### Lazy-load contract

`anime.js` must never load on the auth screen.

- `motion.js` is imported only by components inside `AppView`, which is already `lazy()` in `App.jsx`.
- The auth screen never imports anything from `motion.js`.
- Verification: bundle analyzer (or simple `dist/assets/*.js` size check after `npm run build`) confirms an `auth-screen` chunk free of `animejs`.

### Per-component animation pattern

Each component owns its own anime.js call inside an effect. Cleanup runs in the effect return. No global animation orchestrator. Example shape:

```jsx
useEffect(() => {
  const anim = safeAnimate(ref.current, { ... });
  return () => anim?.pause?.();  // anime.js v4 returns a controllable instance
}, [trigger]);
```

## Per-feature spec

### 1. Route line draw-on

**Surface:** `MapView.jsx` route layer.
**Trigger:** `useRouting()` resolves with a non-cached route.
**Tool:** anime.js animating `line-gradient` step on the Mapbox layer (or `stroke-dashoffset` on an SVG overlay if the route is rendered as SVG).
**Spec:** 700ms, ease-out-soft, runs once. Re-animation suppression: `MapView` keeps a ref to the last-rendered route's coordinates; if a new `useRouting()` result has identical coordinates (deep equality), skip the draw-on and render the route immediately. This handles the cache-hit case without requiring a backend change to the response shape.

### 2. Countdown digits

**Surface:** countdown banner in `MapView.jsx` and `RaceEditor.jsx`.
**Trigger:** every second tick from `useCountdown`.
**Tool:** anime.js per-digit transform.
**Spec:** each digit slides up 1em (translateY) at the second-roll-over with a quick fade. 200ms, ease-out-soft. Falls back to plain text under `prefers-reduced-motion` (the `safeAnimate` guard).

### 3. Banner improvement % and minutes count-up

**Surface:** `BetterRouteBanner.jsx`.
**Trigger:** `alternative` payload arrives from `useRouteNotifications`.
**Tool:** anime.js numeric tween, target is a state value rendered into the DOM.
**Spec:** 0 → final value over `MOTION_SLOW` (600ms), ease-out-soft. Both numbers tween in parallel as part of a single timeline so they finish together.

### 4. Isochrone "thinking" pulse

**Surface:** `RouteControls.jsx` (or wherever the start mark is rendered while computing).
**Trigger:** `useRouting().loading === true`.
**Tool:** **CSS `@keyframes pulseRing`** — *not* anime.js. An indefinite loop animation is exactly what CSS keyframes are best at; anime.js would be overkill.
**Spec:** soft accent-colored ring scales 1 → 1.6 with opacity 0.6 → 0, infinite, 1.4s per cycle, ease-out. Stops when loading flips false.

### 5. Mark drop

**Surface:** `RaceEditor.jsx` per-mark renderer.
**Trigger:** new mark added to the course.
**Tool:** anime.js.
**Spec:** scale 0.4 → 1.0 with `EASE_OUT_OVERSHOOT`, 250ms. Only the newly-added mark animates; existing marks don't re-trigger.

### 6. Auth → app intro

**Surface:** `AppView.jsx` on first mount of a session.
**Trigger:** lazy chunk loads after auth resolves.
**Tool:** anime.js timeline.
**Spec (~900ms total):**
- 0–250ms: paper-color sweep clears (decorative element fades out).
- 200–700ms: a faint sample route traces across the empty map (decorative SVG layer over Mapbox).
- 400–900ms: wind barbs fade in to opacity 1.

Runs once per session (gate via `sessionStorage["intro_played"]`). Skipped if user is loading directly into an active-race state — the intro is for the "I just signed in" moment, not for reloads mid-race.

### 7. Geolocation auto-pan ("follow me")

**Surface:** `MapView.jsx`.
**State:** `useFollowMode(raceId)` (new hook).
**Tool:** **Mapbox `map.easeTo()`** — *not* anime.js. Driving the Mapbox camera with anime.js would fight Mapbox's internal animation loop.
**Spec:**
- When `following === true` and `useGeolocation` emits a new position, call `map.easeTo({ center: [lon, lat], duration: 600, easing: ease-out })`.
- Throttle to ≤1 camera move per second even if geolocation fires faster (accuracy bounce protection).
- User-initiated camera changes set `following = false`. Wire to Mapbox events: `dragstart`, `zoomstart` (but only if user-initiated — `e.originalEvent` truthy), `rotatestart`, `wheel`.
- A "Re-center" pill button (CSS, not anime.js, fade-in 150ms) appears when `following === false && currentPosition !== null`. Tapping calls `recenter()`, which flips `following = true` and pans to current position.
- Default state and persistence: see `useFollowMode` description in Architecture above (default `true` when `raceId` non-null and no prior `sessionStorage` value, persisted per `raceId`).

**Bearing handling (course-up vs north-up):**
- Default: north-up (don't rotate the map).
- Course-up is a separate user preference (out of scope for ship-1; reserved as `localStorage["map.bearingMode"]` for a future settings toggle). For ship-1, `easeTo` does not include a `bearing` argument.

## Performance contract

These are non-negotiable. They're the reason this design uses CSS where it can and lazy-loads anime.js.

1. **Auth screen bundle is unaffected.** Anime.js loads only inside `AppView`'s lazy chunk.
2. **`safeAnimate` wraps every anime.js call.** Bails on `prefers-reduced-motion` and on hidden tab.
3. **Targets are transform/opacity only.** Never animate width/height/top/left/margin (would trigger layout). This is a code-review rule, not enforced in code.
4. **No anime.js drives Mapbox camera.** Camera is Mapbox's domain; anime.js coordinates DOM/SVG overlay only.
5. **No animation runs while a Mapbox interaction is active.** The follow-mode gesture handlers already cancel `easeTo`; for DOM animations, this isn't a concern (separate compositor layers).
6. **Battery: hidden-tab guard.** `safeAnimate` checks `document.visibilityState`. Sailors on backgrounded tabs shouldn't burn battery on count-ups.

## Accessibility

- `prefers-reduced-motion: reduce` already short-circuits CSS animations via `index.css:120-128`.
- `safeAnimate` extends the same to anime.js: bail and snap to end state.
- The "Re-center" pill is a real `<button>` with `aria-label="Re-center on my position"`.
- Count-up tweens preserve final accessibility-tree value (the DOM ends at the same number it would have rendered statically). Screen readers announce the final state, not the intermediate frames.

## Testing

- **Manual smoke per animation** in dev with `prefers-reduced-motion` off, then on (Chrome DevTools → Rendering → Emulate CSS media features).
- **Bundle size check** after `npm run build` to confirm auth-screen chunk has no `animejs`.
- **Existing tests stay green.** `useRouteNotifications` tests assert payload handling; no animation-behavior tests in jsdom (low value, hard to assert).
- **`useFollowMode` unit tests** (jest-style if a test runner is added later; not required for ship-1 since the frontend has no test setup today): verify `following` state transitions on `recenter()` and on simulated user-gesture events.

## Open follow-ups (not in this spec)

- AI tactician suggestion animations — when that feature ships.
- Course-up bearing toggle — when settings UI exists.
- Leg-list auto-scroll into view — when a leg list ships.
- A frontend test harness — currently no `lint`/`test` scripts; if added later, motion behavior would still be tested manually but `useFollowMode` could be unit-tested.

## File-touch summary

```
NEW   frontend/src/lib/motion.js
NEW   frontend/src/hooks/useFollowMode.js
EDIT  frontend/src/index.css                       (motion tokens)
EDIT  frontend/src/glass.css                       (token refs, pulseRing keyframe)
EDIT  frontend/src/components/MapView.jsx          (follow-mode wiring, recenter pill)
EDIT  frontend/src/components/BetterRouteBanner.jsx (count-ups)
EDIT  frontend/src/components/RouteControls.jsx    (thinking pulse)
EDIT  frontend/src/RaceEditor.jsx                  (mark drop animation)
EDIT  frontend/src/AppView.jsx                     (intro timeline)
EDIT  frontend/package.json                        (animejs dependency)
```
