// app.config.js — dynamic Expo config.
//
// Extends the static base in app.json (passed in as `config`) and layers
// on the native module wiring that Phase 1 needs:
//
//   * expo-dev-client          — required: Transistorsoft is a native
//                                module, so Expo Go can't load it. We run
//                                a custom dev client built by EAS.
//   * react-native-background-geolocation (Transistorsoft) — the capture
//                                engine. The config plugin injects the
//                                Android foreground-service + background
//                                location permissions and the iOS
//                                location background mode at prebuild.
//                                The runtime license key is read from an
//                                EAS secret (never committed); the trial
//                                works in the dev client without it. The
//                                tsbackgroundfetch native AAR ships
//                                inside this package's android/libs/ — do
//                                NOT install react-native-background-fetch
//                                as a direct dependency; it would build
//                                as its own Gradle subproject and fail to
//                                resolve com.transistorsoft:tsbackgroundfetch.
//   * expo-gradle-ext-vars      — Transistorsoft's required Expo companion.
//                                Sets the Android Gradle ext vars
//                                (Play Services location + tslocationmanager
//                                versions) without manual gradle edits.
//   * @react-native-google-signin/google-signin — native Google Sign-In.
//                                Uses native Play Services (no browser-redirect
//                                OAuth flow), which is why we picked this over
//                                expo-auth-session: the SDK matches the calling
//                                app to a Google OAuth Android client via
//                                package name + signing-key SHA-1 at the OS
//                                level, no redirect_uri configuration needed.
//                                The plugin only needs an iosUrlScheme for iOS
//                                builds; Android works without extra config.
//
// iOS background-location specifics (UIBackgroundModes + purpose strings)
// and Android config live under ios/android below. Runtime notification
// copy + tracking tuning live in src/recorder/backgroundGeolocation.ts
// (the plugin handles build-time native config; .ready() handles runtime).

module.exports = ({ config }) => ({
  ...config,

  ios: {
    ...config.ios,
    infoPlist: {
      ...(config.ios && config.ios.infoPlist),
      // Run location updates while backgrounded / screen-locked. `fetch`
      // pairs with react-native-background-fetch.
      UIBackgroundModes: ["location", "fetch"],
      // Purpose strings shown in the iOS permission prompts. Plain,
      // honest language — App Review reads these against actual behavior.
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
    // The Transistorsoft config plugin adds the location + foreground-
    // service permissions; these are listed explicitly for clarity and to
    // cover Android 14's typed foreground-service permission.
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
    "expo-dev-client",
    [
      "react-native-background-geolocation",
      // Pass the license ONLY when set. Setting `license: undefined`
      // causes the plugin to write the literal string "UNDEFINED" into
      // AndroidManifest.xml, which the runtime SDK then rejects as a
      // bad license ("LICENSE VALIDATION FAILURE — Invalid license key:
      // UNDEFINED"). Omitting the field entirely lets the SDK fall to
      // trial mode in debug/dev-client builds. Set via:
      //   eas secret:create --name TRANSISTOR_LICENSE --value <key>
      // ...before a preview/production build.
      process.env.TRANSISTOR_LICENSE
        ? { license: process.env.TRANSISTOR_LICENSE }
        : {},
    ],
    // No-op plugin today, but kept per Transistorsoft convention. The package
    // itself MUST be a direct ^4.4.x dependency (see package.json) so it
    // hoists to root node_modules and brings the newer tsbackgroundfetch AAR
    // that this version's gradle expects. The 4.2.x branch bg-geo prefers
    // requires com.transistorsoft:tsbackgroundfetch:1.0.4 which lives only
    // inside its nested libs/ folder and which Expo's settings.gradle
    // (FAIL_ON_PROJECT_REPOS) does not register. expo-doctor will warn about
    // a duplicate background-fetch; that warning is load-bearing — leave it.
    "react-native-background-fetch",
    [
      "expo-gradle-ext-vars",
      {
        // Versions documented by Transistorsoft's setup guide.
        // tslocationmanager 4.0.+ pulls the latest 4.0.x at build time.
        googlePlayServicesLocationVersion: "21.3.0",
        tslocationmanagerVersion: "4.0.+",
      },
    ],
    // Android-only: no plugin config needed because Google Play Services
    // matches the calling app to its Android OAuth client via package name +
    // SHA-1 at the OS level. The iosUrlScheme key will need to be added here
    // when we cut iOS (set it to the iOS OAuth client's reversed client ID).
    "@react-native-google-signin/google-signin",
  ],
});
