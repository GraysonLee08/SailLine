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
// SECRETS — prebuild pivot 2026-06-02
// -----------------------------------
// We moved off EAS to local Gradle. Secrets that used to be EAS secrets
// (MAPBOX_DOWNLOAD_TOKEN, TRANSISTOR_LICENSE) now live in
// ~/.gradle/gradle.properties (user-global, NOT committed). The Gradle
// plugins below read them via project.findProperty('NAME') at build
// time, NOT via process.env here. That decoupling means:
//
//   * `expo prebuild` does NOT need the env vars set to succeed.
//   * `gradlew assembleDebug` reads them on every build.
//   * Forgetting to set them surfaces as a Gradle error, not a silent
//     "UNDEFINED" baked into the generated config.
//
// See sailline-docs/2026-06-02_C1-prebuild-runbook.md for the one-time
// setup of those properties.

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
    // Transistorsoft react-native-background-geolocation
    //
    // Plugin config no longer takes the license here. With this argument
    // omitted, prebuild does NOT bake a license string into
    // android/app/src/main/res/values/strings.xml. Instead, the Gradle
    // companion (expo-gradle-ext-vars below) emits a buildscript hook
    // that reads `TRANSISTOR_LICENSE` from gradle.properties at build
    // time and exposes it to the SDK's manifest-placeholder mechanism.
    //
    // If you want to override this locally without touching gradle.properties
    // (e.g., for a quick dev test), set the env var before prebuild:
    //   $env:TRANSISTOR_LICENSE = "..."
    //   npx expo prebuild
    // The conditional below catches that case for parity with the prior
    // behaviour.
    [
      "react-native-background-geolocation",
      process.env.TRANSISTOR_LICENSE
        ? { license: process.env.TRANSISTOR_LICENSE }
        : {},
    ],
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
