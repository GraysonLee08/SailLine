// app.config.js — dynamic Expo config.
//
// Extends the static base in app.json (passed in as `config`) and layers
// on the native module wiring that Phase 1 + the blended auto-start +
// Phase 3 (map shell + routing) require:
//
//   * expo-router               — file-based router. Entry point is set
//                                 in package.json:main = "expo-router/entry".
//                                 The plugin block here is required so the
//                                 prebuild wires deep-linking + native nav
//                                 dependencies (react-native-screens +
//                                 react-native-safe-area-context).
//   * expo-dev-client           — required: Transistorsoft is a native
//                                 module, so Expo Go can't load it. We run
//                                 a custom dev client.
//   * react-native-background-geolocation (Transistorsoft) — the capture
//                                 engine.
//   * expo-gradle-ext-vars      — Transistorsoft's required companion.
//   * @react-native-google-signin/google-signin — native Google Sign-In.
//   * expo-notifications        — T-6 reminder paired with the T-5
//                                 BackgroundFetch fallback.
//   * @rnmapbox/maps            — map canvas.
//
// SECRETS & LICENSES — prebuild pivot 2026-06-02, license fix 2026-06-05
// ----------------------------------------------------------------------
// Three distinct values with DIFFERENT handling — don't conflate them:
//
//   * MAPBOX_DOWNLOADS_TOKEN (secret `sk.` token) — a real secret. Read
//     from ~/.gradle/gradle.properties by the @rnmapbox Gradle plugin at
//     BUILD time. Never committed, never shipped in the APK.
//
//   * EXPO_PUBLIC_MAPBOX_TOKEN (public `pk.` token) — read by JS at
//     runtime from .env.local; ships in the APK (restrict it by app/URL
//     in the Mapbox dashboard).
//
//   * TRANSISTOR_LICENSE — NOT a secret. The Transistorsoft license is
//     bound to this app's applicationId and is embedded in every APK's
//     AndroidManifest, so it cannot be kept out of a shipped build. It is
//     injected at PREBUILD time into the manifest via the plugin config
//     below. The gradle.properties entry is NOT consumed for the license
//     (that path was a myth — a clean prebuild without it bakes the literal
//     "UNDEFINED" into the manifest, which fails license validation in
//     RELEASE builds and silently disables tracking). We therefore commit
//     the license as a constant below so every prebuild is deterministic.
//
// See sailline-docs/2026-06-02_C1-prebuild-runbook.md and
// sailline-docs/testing-and-deployment-checklist.md.

// Committed Transistorsoft license. Safe to commit (see note above): it is
// not a credential, only validates against this app's applicationId, and
// already ships inside every distributed APK. Replace the placeholder with
// your key. An env var still overrides it for one-off builds.
const TRANSISTOR_LICENSE =
  process.env.TRANSISTOR_LICENSE ||
  "eyJhbGciOiJFZERTQSIsImtpZCI6ImVkMjU1MTktbWFpbi12MSJ9.eyJvcyI6ImFuZHJvaWQiLCJhcHBfaWQiOiJjb20uc2FpbGxpbmUuYXBwIiwib3JkZXJfbnVtYmVyIjoxNjM4NCwicmVuZXdhbF91cmwiOiJodHRwczovL3Nob3AudHJhbnNpc3RvcnNvZnQuY29tL2NhcnQvMTY1MDc4NjE1MDU6MT9ub3RlPTEwODYyIiwiY3VzdG9tZXJfaWQiOjk4NzAsInByb2R1Y3QiOiJyZWFjdC1uYXRpdmUtYmFja2dyb3VuZC1nZW9sb2NhdGlvbiIsImtleV92ZXJzaW9uIjoxLCJhbGxvd2VkX3N1ZmZpeGVzIjpbIi5kZXYiLCIuZGV2ZWxvcG1lbnQiLCIuc3RhZ2luZyIsIi5zdGFnZSIsIi5xYSIsIi51YXQiLCIudGVzdCIsIi5kZWJ1ZyJdLCJtYXhfYnVpbGRfc3RhbXAiOjIwMjcwNjI3LCJncmFjZV9idWlsZHMiOjAsImVudGl0bGVtZW50cyI6WyJjb3JlIl0sImlhdCI6MTc3OTkwMjY3Nn0.cF3q-jT3jKHpNgFOkcrEbkwvZKfXPZuUQiVuwOJXY672JrQacfeVES-oqOUeEAa5rpMJrRtaDBQBDdojUz1WBg";

// FCM for server-initiated push (dead-recorder watchdog, 2026-07-05).
// google-services.json comes from the Firebase console (Project settings
// → Your apps → Android app com.sailline.app → download) and is safe to
// commit — it contains identifiers, not credentials. Conditional so
// prebuild keeps working before the file is downloaded; without it,
// device push registration no-ops (see src/notifications/pushTokens.ts).
const fs = require("fs");
const GOOGLE_SERVICES = fs.existsSync(`${__dirname}/google-services.json`)
  ? { googleServicesFile: "./google-services.json" }
  : {};

module.exports = ({ config }) => ({
  ...config,

  ios: {
    ...config.ios,
    infoPlist: {
      ...(config.ios && config.ios.infoPlist),
      // Run location updates while backgrounded / screen-locked. `fetch`
      // pairs with react-native-background-fetch (used by both the
      // recorder and the T-5 auto-start fallback task).
      UIBackgroundModes: ["location", "fetch"],
      NSLocationWhenInUseUsageDescription:
        "SailLine records your boat's track during a race.",
      NSLocationAlwaysAndWhenInUseUsageDescription:
        "SailLine records your boat's track during a race, including while " +
        "your screen is off, so your route and performance are captured " +
        "continuously.",
    },
  },

  android: {
    ...config.android,
    ...GOOGLE_SERVICES,
    permissions: [
      "ACCESS_COARSE_LOCATION",
      "ACCESS_FINE_LOCATION",
      "ACCESS_BACKGROUND_LOCATION",
      "FOREGROUND_SERVICE",
      "FOREGROUND_SERVICE_LOCATION",
      "WAKE_LOCK",
    ],
  },

  plugins: [
    ...(config.plugins || []),
    "expo-router",
    "expo-dev-client",
    // Transistorsoft react-native-background-geolocation.
    //
    // Always inject the license (see the TRANSISTOR_LICENSE constant + note
    // at the top of this file). Passing it here is what writes the
    // <meta-data com.transistorsoft.locationmanager.license> value into
    // AndroidManifest.xml during prebuild. Never let this be empty/omitted
    // or the manifest gets "UNDEFINED" and RELEASE builds fail license
    // validation (tracking silently disabled → no recorded points).
    ["react-native-background-geolocation", { license: TRANSISTOR_LICENSE }],
    "react-native-background-fetch",
    [
      "expo-gradle-ext-vars",
      {
        googlePlayServicesLocationVersion: "21.3.0",
        // The wrapper jumping JS major (v4→v5) does NOT drag the native
        // SDK to v5 — latest tslocationmanager on Maven is 4.1.6 as of
        // 2026-05-27 and the v5 wrapper Setup docs still call out
        // "4.0.+" for this var. Initial v5 upgrade attempt bumped this
        // to "5.0.+" and the gradle dependency resolver couldn't find
        // a matching artifact (it doesn't exist).
        tslocationmanagerVersion: "4.0.+",
      },
    ],
    "@react-native-google-signin/google-signin",
    [
      "expo-notifications",
      {
        // Default channel + icon: runtime config in
        // src/recorder/scheduledAutoStart.ts.
      },
    ],
    [
      "@rnmapbox/maps",
      {
        // No RNMapboxMapsDownloadToken here.
        //
        // Previously this read process.env.MAPBOX_DOWNLOAD_TOKEN and baked
        // the secret into android/build.gradle on prebuild. Since the
        // android/ tree is now committed, baking a secret into it is a
        // security incident waiting to happen.
        //
        // The @rnmapbox/maps Gradle plugin already resolves
        // MAPBOX_DOWNLOADS_TOKEN from project properties (gradle.properties)
        // when not passed explicitly here. Put it in
        // ~/.gradle/gradle.properties — see C1 runbook.
        //
        // Verify the token is being read by running:
        //   cd mobile/android; .\gradlew :app:dependencies | findstr mapbox
        // If the resolution fails with 401, the property is missing or the
        // token lacks the DOWNLOADS:READ scope (must start with `sk.`).
      },
    ],
  ],
});
