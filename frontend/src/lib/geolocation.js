// geolocation.js — platform-adaptive GPS watcher.
//
// In a pure web context (sailline.web.app, dev server) this calls
// navigator.geolocation.watchPosition. When the app is loaded inside a
// Capacitor native shell (Android), it uses the
// @capacitor-community/background-geolocation plugin, which keeps
// capturing fixes while the screen is locked via an Android foreground
// service + persistent notification.
//
// The hook (useTrackRecorder) consumes one normalised point shape:
//   { recorded_at, lat, lon, speed_kts, heading_deg }
// regardless of which platform produced it. All shape translation lives
// here.
//
// Native detection deliberately uses `window.Capacitor` rather than
// importing from '@capacitor/core' so this module compiles cleanly even
// when Capacitor packages are not installed (e.g. fresh checkout, web-
// only dev). The background-geolocation plugin is reached the same
// way — through `window.Capacitor.Plugins.BackgroundGeolocation`, the
// global registry Capacitor populates at native boot. We deliberately
// do NOT `import` it: any module specifier (even a dynamic one) is
// resolved by Vite's import-analysis at transform time, which would
// fail every web build until the native deps are installed.

const MS_TO_KTS = 1.943844;

// Same defaults the old useTrackRecorder used. Kept in the adapter so
// callers don't need to know about platform-specific tuning.
export const DEFAULT_WATCH_OPTIONS = Object.freeze({
  enableHighAccuracy: true,
  timeout: 15_000,
  maximumAge: 0,
});

/**
 * True when running inside a Capacitor native shell. Safe to call in
 * a browser — returns false when window.Capacitor is absent.
 */
export function isNative() {
  if (typeof window === "undefined") return false;
  const cap = window.Capacitor;
  if (!cap || typeof cap.isNativePlatform !== "function") return false;
  try {
    return cap.isNativePlatform();
  } catch {
    return false;
  }
}

/**
 * Normalise a raw position object to the canonical point shape.
 * Exported for unit tests.
 *
 * @param {object} pos    raw position from either source
 * @param {"web"|"native"} source
 */
export function normalizePosition(pos, source) {
  if (source === "native") {
    // @capacitor-community/background-geolocation Location shape:
    //   { latitude, longitude, accuracy, altitude, altitudeAccuracy,
    //     simulated, speed, bearing, time }
    // speed: m/s (or null), bearing: degrees true (or null), time: ms epoch.
    return {
      recorded_at: new Date(
        Number.isFinite(pos.time) ? pos.time : Date.now(),
      ).toISOString(),
      lat: pos.latitude,
      lon: pos.longitude,
      speed_kts: Number.isFinite(pos.speed) ? pos.speed * MS_TO_KTS : null,
      heading_deg: Number.isFinite(pos.bearing) ? pos.bearing : null,
    };
  }
  // Web GeolocationPosition: { coords: {latitude, longitude, speed,
  // heading, accuracy, ...}, timestamp }
  return {
    recorded_at: new Date(pos.timestamp).toISOString(),
    lat: pos.coords.latitude,
    lon: pos.coords.longitude,
    speed_kts: Number.isFinite(pos.coords.speed)
      ? pos.coords.speed * MS_TO_KTS
      : null,
    heading_deg: Number.isFinite(pos.coords.heading)
      ? pos.coords.heading
      : null,
  };
}

/**
 * Start a position watcher.
 *
 * @param {object}   args
 * @param {(point: object) => void}  args.onPosition   called with a
 *   normalised point on each fix.
 * @param {(err: Error) => void}     [args.onError]    optional error sink.
 * @param {object}                   [args.options]    overrides for
 *   DEFAULT_WATCH_OPTIONS (web path) and native notification copy
 *   (native path).
 *
 * @returns {Promise<{stop: () => Promise<void>}>}  call stop() to tear
 *   down the watcher. Always async so the web and native paths share
 *   one shape.
 */
export async function createWatcher({ onPosition, onError, options = {} }) {
  if (typeof onPosition !== "function") {
    throw new TypeError("createWatcher: onPosition must be a function");
  }
  if (isNative()) {
    return createNativeWatcher({ onPosition, onError, options });
  }
  return createWebWatcher({ onPosition, onError, options });
}

function createWebWatcher({ onPosition, onError, options }) {
  if (
    typeof navigator === "undefined" ||
    !navigator.geolocation ||
    typeof navigator.geolocation.watchPosition !== "function"
  ) {
    onError?.(new Error("Geolocation is not supported on this device."));
    return { stop: async () => {} };
  }

  const merged = { ...DEFAULT_WATCH_OPTIONS, ...options };
  let watchId = null;
  try {
    watchId = navigator.geolocation.watchPosition(
      (pos) => {
        try {
          onPosition(normalizePosition(pos, "web"));
        } catch (e) {
          onError?.(e);
        }
      },
      (err) => onError?.(err),
      {
        enableHighAccuracy: merged.enableHighAccuracy,
        timeout: merged.timeout,
        maximumAge: merged.maximumAge,
      },
    );
  } catch (e) {
    onError?.(e);
    return { stop: async () => {} };
  }

  return {
    stop: async () => {
      if (watchId === null) return;
      try {
        navigator.geolocation.clearWatch(watchId);
      } catch {
        /* swallow — best effort */
      }
      watchId = null;
    },
  };
}

async function createNativeWatcher({ onPosition, onError, options }) {
  // Capacitor populates window.Capacitor.Plugins at native boot. Using
  // the registry avoids an ES-module import that Vite would fail to
  // resolve in web-only builds. The plugin's native Android code is
  // still installed via Gradle (the JS side here is just a proxy).
  const BackgroundGeolocation =
    window?.Capacitor?.Plugins?.BackgroundGeolocation;
  if (!BackgroundGeolocation) {
    onError?.(
      new Error(
        "Background-geolocation plugin not registered. Confirm @capacitor-community/background-geolocation is installed and that `npx cap sync android` has been run.",
      ),
    );
    return { stop: async () => {} };
  }

  let watcherId = null;
  try {
    watcherId = await BackgroundGeolocation.addWatcher(
      {
        // Android persistent notification copy.
        backgroundTitle:
          options.backgroundTitle ?? "SailLine — recording track",
        backgroundMessage:
          options.backgroundMessage ??
          "Capturing position for your race.",
        // Prompt for permission if not yet granted. The plugin handles
        // the "always" vs "while in use" distinction internally.
        requestPermissions: options.requestPermissions ?? true,
        // Reject fixes the OS marks as stale.
        stale: options.stale ?? false,
        // 0 = emit every fix; the recorder controls flushing rate.
        distanceFilter: options.distanceFilter ?? 0,
      },
      (location, err) => {
        if (err) {
          if (err.code === "NOT_AUTHORIZED") {
            onError?.(
              new Error(
                "Location permission denied. Enable it in Android Settings → SailLine → Permissions.",
              ),
            );
            return;
          }
          onError?.(err instanceof Error ? err : new Error(String(err)));
          return;
        }
        if (!location) return;
        try {
          onPosition(normalizePosition(location, "native"));
        } catch (e) {
          onError?.(e);
        }
      },
    );
  } catch (e) {
    onError?.(e);
    return { stop: async () => {} };
  }

  return {
    stop: async () => {
      if (watcherId === null) return;
      try {
        await BackgroundGeolocation.removeWatcher({ id: watcherId });
      } catch {
        /* swallow — best effort */
      }
      watcherId = null;
    },
  };
}
