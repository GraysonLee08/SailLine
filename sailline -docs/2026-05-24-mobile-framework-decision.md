# ADR — 2026-05-24 — Cross-platform mobile framework: React Native over Flutter

**Status:** Accepted
**Decision owner:** Grayson (solo dev)
**Supersedes (partially):** the 2026-05-14 Capacitor-wrapper approach (`2026-05-14-session-summary-capacitor.md`) — see *Relationship to prior work*.

## Context

The PWA cannot track GPS/telemetry while the screen is locked — a hard browser limitation, not a bug. Field tests confirmed it: the only clean captures (the 2026-05-15 sessions in `track_points`) happened with the screen on; auto-start (`useAutoStartRecorder`) likewise can't survive backgrounding because `setTimeout` is throttled when the tab is hidden.

A first attempt (2026-05-14) wrapped the existing React app in Capacitor with the free `@capacitor-community/background-geolocation` plugin. The code-side adapter landed (`lib/geolocation.js`, `useTrackRecorder` rewired through `createWatcher`), but the native shell was never completed: no `@capacitor/*` deps in `package.json`, no `frontend/android/`, and the screen-locked smoke test (the stated acceptance criterion) never ran.

We are now committing to a true cross-platform **native** mobile app and evaluated the two industry standards: **React Native** (JS/TS) and **Flutter** (Dart).

## Product scope (both targets share a backend)

**Mobile app (on the water):** boat setup/management, crew setup/management, race creation, pre-race routing, **in-race rerouting from GPS + telemetry**, **AI tactician**, **background GPS/telemetry capture (screen off)**.

**Web app (desk):** boat setup/management, crew setup/management, race creation, pre-race routing, **post-race summary/analysis**.

Boat management, crew management, race creation, and pre-race routing are **shared** across both. Background capture, in-race rerouting, and the live AI tactician are mobile-only; post-race analysis is web-only.

## Decision

Build the mobile app in **React Native**.

## Rationale, against the four guiding principles

| Principle | React Native | Flutter | Edge |
|---|---|---|---|
| **1. Reuse existing work** | Backend (FastAPI/PostGIS routing math) is reached over REST/SSE — reuse is identical either way. Client logic is JS: `lib/latlon.js`, `morfMarks.js`/`morfCourses.js`, `windBarb.js`, `regions.js`, `boatClasses.js`, `markRounding.js`, `imuAxes.js`, `motion.js`, plus the recorder queue/flush and SSE patterns — all port to RN with little change and can become a **shared package** with the web app. | Same backend reuse. But every JS client module is a **from-scratch Dart rewrite**, and the web/mobile shared features (boat/crew/race/routing) get maintained twice in two languages — permanent duplication. | **React Native** |
| **2. Background capture, screen off** | `react-native-background-geolocation` (Transistorsoft) — native `CLLocationManager` / Android foreground service. | `flutter_background_geolocation` — **same vendor (Transistorsoft)**, same native core. | **Tie** |
| **3. Real-time routing / rerouting** | Backend-driven (`POST /api/routing/compute` + SSE better-route channel); client renders on a map. `@rnmapbox/maps` is mature and battle-tested; team already thinks in Mapbox GL terms. | Same API model. Official Mapbox Flutter SDK is younger / historically rockier. | **React Native (slight)** |
| **4. AI tactical advice in-race** | Backend-generated (`race_summary.py` path); client displays. | Identical. | **Tie** |

Flutter's genuine strength — high-performance custom UI rendering — does not address this app's real bottlenecks, which are **server-side numpy isochrone compute** and the **native Mapbox SDK**. Choosing Flutter would pay a full Dart rewrite for an advantage this product doesn't lean on.

The dual-app scope amplifies principle #1: with four features shared between web and mobile, a single JS/TS client codebase (shared pure logic + data + wire formats) is a structural win that Flutter cannot match.

## Consequences

**Positive**
- One language (JS/TS) across web review app + mobile capture app; shared, single-source client logic and data (marks, courses, regions, boat classes, lat/lon parsing).
- Background tracking via the industry gold-standard plugin, available on both platforms.
- Mapbox parity with existing mental model and layer data prep.

**Negative / accepted**
- RN reuses *logic*, not *UI*: JSX views (`MapView.jsx`, layer components) are rewritten in native components. Pure functions and data behind them are reused.
- New native build/release toolchain (Xcode + Android Studio, signing, store submissions) on top of the existing Cloud Build/Firebase pipeline.
- iOS native builds require macOS — same constraint that scoped the 2026-05-14 work to Android-only. Decide Mac access before committing to an iOS timeline.

## Platform compliance (must-do, not optional)

- **Android:** background location requires a **foreground service with a persistent notification** ("SailLine is tracking your route") and exemption from battery optimization on aggressive OEMs (Xiaomi/OnePlus/some Samsung). Transistorsoft handles the service; battery-optimizer prompt is on us.
- **iOS:** declare `UIBackgroundModes` = location, request **Always** authorization with a clear purpose string, and prepare an App Review justification for background location.

## Relationship to prior work

This supersedes the Capacitor *delivery mechanism* from 2026-05-14, not the analysis behind it. The adapter pattern in `lib/geolocation.js` and the `track_points` wire format remain valid and largely portable to RN.

**License reversal (deliberate):** the 2026-05-14 decision chose the *free* community geolocation plugin to avoid the ~$300/platform Transistorsoft license. For a race-critical app where a dropped track ruins the session, we now accept the paid Transistorsoft plugin on both platforms. Cost is justified by reliability; revisit only if a free plugin proves equivalent in on-water testing.

## Tech debt flagged

| Item | Why it's debt | When to address |
|---|---|---|
| Native builds not in CI | Cloud Build builds web only; native regressions caught only by manual device testing. | When release cadence picks up; GitHub Actions has macOS + Android runners. |
| Capacitor `android/` half-state | Code adapter exists but no native shell; risk of confusing future readers. | Clean up / repurpose when the RN project is scaffolded. |
| Two UI codebases (web React + RN) | Shared *logic* is single-source, but two UI layers drift over time. | Enforce the shared-logic package boundary from day one. |
| iOS deferred if no Mac | iOS sailors get no background tracking until a Mac-based build ships. | Resolve Mac access; sequence iOS after Android smoke test passes. |

## Open questions

1. Phone split among target sailors (iPhone vs Android) — drives whether iOS is fast-follow or parallel.
2. Mac access for iOS builds.
3. Monorepo layout for the shared JS/TS client package (web + RN consuming one source of truth, mirroring the existing `regions.py` ↔ `regions.js` discipline).
