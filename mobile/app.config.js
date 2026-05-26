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
//                                 a custom dev client built by EAS.
//   * react-native-background-geolocation (Transistorsoft) — the capture
//                                 engine.
//   * expo-gradle-ext-vars      — Transistorsoft's required companion.
//   * @react-native-google-signin/google-signin — native Google Sign-In.
//   * expo-notifications        — T-6 reminder paired with the T-5
//                                 BackgroundFetch fallback.
//   * @rnmapbox/maps            — map canvas. Requires a build-time
//                                 download token (Mapbox secret access
//                                 token, scoped DOWNLOADS:READ) for the
//                                 private Maven repo + a runtime public
//                                 token used by the JS SDK at runtime.
//                                 Both come from EAS secrets:
//                                   MAPBOX_DOWNLOAD_TOKEN — build-time
//                                   EXPO_PUBLIC_MAPBOX_TOKEN — runtime (pk.*)
//                                 The plugin block below uses the
//                                 download token; the runtime token is
//                                 read by src/components/MapCanvas.tsx via
//                                 process.env.EXPO_PUBLIC_MAPBOX_TOKEN.

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
        // Pass the download token ONLY when set. Mirrors the
        // transistorsoft pattern — an unset value would write the
        // literal "UNDEFINED" into the gradle config and break the
        // private-Maven fetch. With the token unset, the prebuild
        // emits the @rnmapbox plugin config but gradle download
        // resolution will fail; set the EAS secret before building:
        //   eas secret:create --name MAPBOX_DOWNLOAD_TOKEN --value <sk.*>
        // See https://docs.mapbox.com/help/getting-started/access-tokens/
        // for token scopes (secret token with DOWNLOADS:READ enabled).
        ...(process.env.MAPBOX_DOWNLOAD_TOKEN
          ? { RNMapboxMapsDownloadToken: process.env.MAPBOX_DOWNLOAD_TOKEN }
          : {}),
      },
    ],
  ],
});
