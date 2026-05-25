# Mobile development plan — React Native / Expo (2026-05-24)

Builds on the ADR (`2026-05-24-mobile-framework-decision.md`) and the scaffolding plan (`2026-05-24_mobile-scaffolding-plan.md`). Read **Part 0 first** — it's the list of things only you can do. Nothing below it can start until the Part 0 "before coding" items are done.

---

# Part 0 — What YOU (Grayson) must do

I'll write all the code, config, native plugin wiring, and store-submission drafts. But a chunk of this project lives outside the codebase — paid accounts, secrets, app-store paperwork, and anything that runs on your Windows machine, your Apple/Google accounts, or a real boat. Those are yours. Here's the clean split and the exact list.

## The division of labor

**I do:** write/modify all code in the repo, author configs (`eas.json`, `app.config.js`, Metro, workspaces), wire native plugins, port shared logic, write tests, draft store listings and the background-location justification text, and give you the exact commands to run.

**You do:** create and pay for accounts, hold all credentials/secrets, run the CLI commands on your machine (logins, installs, EAS builds tied to your account, store submissions), test on real devices and on the water, and complete store forms that legally require a human identity.

**Why you run the commands, not me:** my code lands in your repo, but the build/test toolchain runs on your Windows machine and on Expo/Apple/Google infrastructure under *your* accounts. My sandbox is a separate Linux box with none of your credentials — and shouldn't have them. So I hand you commands; you run them and paste back output when something fails.

## Accounts & licenses (do these before I start)

| # | Item | Cost | Notes / why |
|---|------|------|-------------|
| 1 | **Apple Developer Program** enrollment | $99/yr | Required for any iOS build, TestFlight, and App Store. **Enrollment can take days** (identity verification) — start now. |
| 2 | **Google Play Console** account | $25 one-time | Required for Android distribution. |
| 3 | **Expo account** (+ EAS) | Free to start | Hosts the cloud builds. Paid tier only if you need more concurrent/faster builds. |
| 4 | **Transistorsoft `react-native-background-geolocation` license** | ~$300 per platform, one-time | Needed for **release** builds (debug works on a trial). This is the paid plugin we chose over the free one — it's the reliability core of the whole app. |
| 5 | **Mapbox** account + access tokens | Usage-based | You likely have this for the web app. Native needs its own public token and a secret **download token** for the SDK. |
| 6 | **Anthropic / Claude API key** | Usage-based | For the AI tactician. Backend already uses it; confirm the key/billing is live. |

## Tooling to install on your Windows machine (before I start)
- Node at the version in `.nvmrc`, and npm.
- Android Studio + Android SDK + an emulator, and a **physical Android phone** with USB debugging on.
- A **physical iPhone** for iOS testing — there is no iOS Simulator without a Mac, so a real device is mandatory.
- You'll use `npx` for Expo/EAS CLIs (no global install needed); just be ready to `eas login` / `expo login`.

## Paperwork only you can complete (before store submission, not before coding)
- A **privacy policy URL** (mandatory for both stores, especially with background location). I can draft the text; you host/own it.
- **Google Play background-location declaration** — Google requires a written justification *and a video demo* of the background use. Human task; I'll prep the script.
- **Apple background-location justification** for App Review + the purpose strings. I draft, you submit.
- Store listing identity: developer legal name/entity, support contact, app icon sign-off, screenshots (I can generate from device captures you take).

## Things only you can physically do (ongoing)
- Run the build/test/install commands I give you and report results.
- **Test on the water** — I can't validate real sailing behavior. You run the on-boat sessions; the key acceptance test (below) is yours to execute.
- Hold and rotate secrets (API keys, signing credentials, tokens). Never paste production secrets into chat — put them in your local env / EAS secrets.

## Money summary
One-time: Google $25 + Transistorsoft (~$300 × platforms you ship). Recurring: Apple $99/yr. Usage-based: Mapbox, Claude API, possibly EAS.

---

# Part 1 — Phased build

Effort sizes are rough (S/M/L), not dates. Each phase lists what I build, what you do, and how we know it's done. Backend is largely already built (FastAPI/PostGIS routing, forecasts, currents, AI summary) — most phases are mobile-client + light backend glue.

## Phase 0 — Scaffolding (S–M)
Per the scaffolding plan: monorepo, `packages/shared`, Expo app, EAS, dev client, retire Capacitor.
- **You:** Part 0 accounts/tooling; run `npm install`, `npx expo start`, and the first `eas build` for both platforms.
- **Done when:** shared package imports on web + mobile; EAS dev-client runs on a real Android device and an EAS iOS build completes; Transistorsoft permission prompt appears.

## Phase 1 — Background GPS capture (L) ← the actual unlock
The whole reason for the pivot. Port the recorder to RN against Transistorsoft: GPS capture that survives screen-lock, offline queue, batched flush to the existing `POST /api/races/{id}/telemetry`, Android foreground-service notification, iOS background-location config.
- **I build:** RN recorder + queue/flush reusing the shared wire format; permission/onboarding flow; the foreground-service notification copy.
- **Backend glue:** CORS for the mobile origin; the long-flagged telemetry contract tests (Known debt #2 in the dev plan) before real data flows.
- **You:** run the **15-minute screen-locked smoke test on a real phone, in motion** — the acceptance criterion that was never met on the web/Capacitor path. Then a real beer-can race capture.
- **Done when:** a screen-locked session produces a continuous, gap-free track in `track_points` end to end.

## Phase 2 — Management feature parity (M)
Boat setup/management, crew setup/management, race creation — shared with web, so this is mostly RN UI over existing endpoints (`/api/boats`, crew, `/api/races`) and shared data (MORF marks/courses, boat classes).
- **I build:** RN screens + data hooks reusing `@sailline/shared`; Firebase Auth login on device.
- **You:** device test the flows; confirm auth/token handling on mobile.
- **Done when:** you can create a boat, manage crew, and create a race entirely on the phone.

## Phase 3 — Pre-race routing on mobile (M–L)
Map-first routing: Mapbox native map, marks/course rendering, `POST /api/routing/compute`, the 425 "too early" handling, and the SSE "better route" channel (`useRouteNotifications` logic ported; note RN needs an EventSource/fetch-event-source equivalent with auth headers).
- **I build:** `@rnmapbox/maps` integration, route rendering, the SSE client for RN, wind-barb rendering (reusing the shared barb geometry).
- **You:** Mapbox native token setup; device test routing against production.
- **Done when:** you can compute and view a pre-race route with marks, wind, and better-route alerts on the phone.

## Phase 4 — In-race rerouting + live telemetry/HUD (L)
Live GPS + IMU (heel/pitch via device sensors), the high-contrast "sunlight" HUD (heel gauge, performance bar), and continuous rerouting from live position. Pulls in the existing PRD Phase 2/4 specs, now native.
- **I build:** IMU sampling on RN, sensor-fusion (Kalman/complementary filter per PRD §3.5), HUD components, live route-refresh wiring.
- **Backend glue:** Target-Actual / virtual-wind inference if not already shipped (PRD Phase 3).
- **You:** on-water testing — heel calibration, sunlight legibility, glove-sized touch targets.
- **Done when:** live heel/performance HUD and rerouting work during a real sail.

## Phase 5 — AI tactician in-race (M)
The "Quiet Cockpit" advisor: Claude-API advice calls ("Tack in 30s to stay in pressure") driven by live telemetry + routing, delivered with a low-latency advice-out channel (the PRD's deferred WebSocket, which is where <500ms actually matters).
- **I build:** RN advice UI + alerts (color/haptic), the advice-out client; backend advisor prompt/channel if not already present.
- **You:** confirm Claude API billing; on-water judgment of advice quality.
- **Done when:** useful, timely tactical calls appear during a real race.

## Phase 6 — Release (M)
Store readiness: TestFlight + Play internal testing, store listings, the background-location declarations, privacy policy, screenshots, and submission.
- **I build:** release EAS profiles, listing/declaration drafts, screenshot generation from your captures.
- **You:** complete and submit the store paperwork (identity-bound), host the privacy policy, record the Play background-location demo video, and click submit.
- **Done when:** builds pass review and are installable from TestFlight / Play.

## Sequencing notes
- Phases 0→1 are the critical path and the highest-risk (native background execution). Prove them before investing in UI.
- Phases 2 and 3 can interleave once Phase 1 is stable.
- The existing web app keeps doing management + pre-race + post-race review throughout — nothing here regresses it.

## Cross-cutting / backend touchpoints
- CORS allow-list for the mobile origin (web used same-origin rewrites; mobile can't).
- Telemetry contract tests (existing Known debt #2) — land before Phase 1 real data.
- Auth: Firebase ID-token flow on device, mirroring the web `apiFetch` pattern.
- The half-finished Capacitor adapter is retired during Phase 0.

## What I need from you to start
The Part 0 "before coding" items (accounts #1–6 and the Windows toolchain). Once those exist and you've confirmed, I begin Phase 0 — and per your workflow, I'll share each phase's plan/outline for your review before writing implementation code.
