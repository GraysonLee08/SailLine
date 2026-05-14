// Capacitor config for the SailLine native (Android) shell.
//
// `webDir` is the Vite build output. `npx cap sync android` copies
// frontend/dist/ into android/app/src/main/assets/public/ each build.
//
// Web continues to ship via Firebase Hosting at sailline.web.app; this
// file is only consulted when running `npx cap` commands locally on
// Windows. CI does not build the native app.
//
// appId is the Android package name (reverse-DNS). Once published to
// Play, changing this requires a new app listing — keep it stable.

import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.sailline.app",
  appName: "SailLine",
  webDir: "dist",
  // https scheme so cookies, service workers, and Firebase Auth behave
  // identically to the deployed PWA. The plugin's WebView still loads
  // from app://, but APIs see a secure origin.
  server: {
    androidScheme: "https",
  },
  android: {
    // Allow `npm run dev` over LAN by relaxing cleartext only when an
    // explicit `server.url` is set. Production builds with no server.url
    // ignore this flag.
    allowMixedContent: false,
  },
  plugins: {
    // Background-geolocation plugin reads its own Android manifest
    // declarations; no JS-side config is required here. Defaults set
    // in lib/geolocation.js (backgroundTitle, backgroundMessage,
    // distanceFilter) carry through addWatcher().
  },
};

export default config;
