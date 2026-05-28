// backgroundGeolocation.ts — Transistorsoft adapter for the RN recorder.
//
// The RN analogue of frontend/src/lib/geolocation.js: it owns ALL shape
// translation between react-native-background-geolocation and the
// recorder's canonical local point shape, plus the start/stop lifecycle
// and the Android foreground-service notification copy.
//
// Design note (deliberate): Transistorsoft ships its own SQLite
// persistence + HTTP auto-POST. We do NOT use it. Its HTTP layer posts
// the plugin's own location schema (not our batched {gps:[...]}
// telemetry shape) and threading hourly-expiring Firebase tokens through
// the native HTTP layer is fragile. So Transistorsoft is the CAPTURE
// ENGINE ONLY; the recorder hook owns queueing, batching, flushing, and
// drop-on-ack (the proven web pattern). See the Phase 1 plan, §2.
//
// v5 upgrade (2026-05-27): Transistorsoft's react-native-background-geolocation
// shipped v5 with a fully restructured Config API. Every flat option from v4
// is now nested under a typed sub-config (geolocation / app / logger / etc.).
// The runtime behavior we want is unchanged; only the shape of the object
// passed to ready() changed. Removed v4-only options: `autoStart` (we always
// called .start() explicitly anyway) and `foregroundService` (implicit in v5).

// IMPORTANT: react-native-background-geolocation v5 only ships a default
// export. The package's index.d.ts re-exports the enum types from
// `@transistorsoft/background-geolocation-types`, which makes named imports
// like `import { DesiredAccuracy } from "react-native-background-geolocation"`
// TYPE-CHECK fine but RESOLVE to `undefined` at runtime — crashing the
// recorder with "Cannot read property 'High' of undefined" the moment
// startWatcher() runs. Always reach the enums through the default export
// (BackgroundGeolocation.DesiredAccuracy.High) so the value and the type
// come from the same place.
import BackgroundGeolocation from "react-native-background-geolocation";
import type {
  Location,
  Subscription,
} from "@transistorsoft/background-geolocation-types";

const MS_TO_KTS = 1.943844;

// Canonical local point shape consumed by the recorder hook. Identical
// to the web adapter's output so everything downstream is shared.
export type LocalPoint = {
  recorded_at: string; // ISO 8601
  lat: number;
  lon: number;
  speed_kts: number | null;
  heading_deg: number | null;
  gps_acc_m: number | null;
};

export type WatcherCallbacks = {
  onPosition: (point: LocalPoint) => void;
  onError?: (err: Error) => void;
};

/**
 * Normalise a Transistorsoft Location into the canonical local point.
 *
 * v5 type notes:
 *   - `speed`, `heading`, `accuracy` are typed `number | undefined`
 *     instead of v4's sentinel `-1` for "unavailable". Runtime semantics
 *     are the same (missing/invalid → drop), but TypeScript needs an
 *     explicit `!= null` to narrow `undefined` out — `Number.isFinite()`
 *     doesn't narrow as a type guard.
 *   - `location.timestamp` is typed `string | number`. React Native has
 *     historically returned ISO 8601 strings, but the type allows
 *     milliseconds too. Coerce through `Date` to land on a single
 *     canonical ISO 8601 string regardless of which the SDK gave us.
 *
 * Exported for unit testing.
 */
export function normalizeLocation(location: Location): LocalPoint {
  const { coords } = location;
  const speed = coords.speed;
  const heading = coords.heading;
  const accuracy = coords.accuracy;
  return {
    recorded_at: new Date(location.timestamp).toISOString(),
    lat: coords.latitude,
    lon: coords.longitude,
    speed_kts:
      speed != null && Number.isFinite(speed) && speed >= 0
        ? speed * MS_TO_KTS
        : null,
    heading_deg:
      heading != null && Number.isFinite(heading) && heading >= 0
        ? heading
        : null,
    gps_acc_m:
      accuracy != null && Number.isFinite(accuracy) && accuracy >= 0
        ? accuracy
        : null,
  };
}

/**
 * Configure Transistorsoft, register the location listener, and start
 * tracking. Resolves once tracking is live. Call the returned handle's
 * stop() to tear everything down.
 *
 * Tuning targets continuous ~1 Hz capture (gap-free is the Phase 1
 * acceptance criterion), so we drive by time, not distance:
 *   - distanceFilter: 0          → don't gate on movement
 *   - locationUpdateInterval     → ~1 s cadence (Android)
 *   - disableElasticity: true    → keep cadence steady (no auto back-off)
 *
 * Note on desiredAccuracy: v4's DESIRED_ACCURACY_NAVIGATION was
 * cross-platform but accepted as an alias for HIGH on Android. In v5,
 * DesiredAccuracy.Navigation is documented as iOS-only, so we use
 * DesiredAccuracy.High — same providers (GPS + Wifi + Cellular), same
 * accuracy class on Android.
 */
export async function startWatcher({
  onPosition,
  onError,
}: WatcherCallbacks): Promise<{ stop: () => Promise<void> }> {
  const locationSub: Subscription = BackgroundGeolocation.onLocation(
    (location) => {
      try {
        onPosition(normalizeLocation(location));
      } catch (e) {
        onError?.(e instanceof Error ? e : new Error(String(e)));
      }
    },
    (error) => {
      // Transistorsoft location-error codes are numeric; surface them.
      onError?.(new Error(`location error ${error}`));
    },
  );

  await BackgroundGeolocation.ready({
    // Top-level: factory-reset before applying so dev iterations always
    // pick up our latest config (not a stale persisted one).
    reset: true,

    // ── Accuracy & cadence (was flat in v4) ──────────────────────────
    geolocation: {
      desiredAccuracy: BackgroundGeolocation.DesiredAccuracy.High,
      distanceFilter: 0,
      locationUpdateInterval: 1000,
      fastestLocationUpdateInterval: 1000,
      disableElasticity: true,
      locationAuthorizationRequest: "Always",
      pausesLocationUpdatesAutomatically: false,
      showsBackgroundLocationIndicator: true,
    },

    // ── App lifecycle + Android foreground service ───────────────────
    // We start/stop explicitly around a race; don't auto-resume on boot
    // or keep running after the app is terminated in Phase 1.
    app: {
      stopOnTerminate: true,
      startOnBoot: false,
      backgroundPermissionRationale: {
        title:
          "Allow SailLine to record your track while the screen is off?",
        message:
          "SailLine needs background location so your race track keeps " +
          "recording with the phone locked in your pocket.",
        positiveAction: "Allow",
        negativeAction: "Cancel",
      },
      notification: {
        title: "SailLine — recording your race",
        text: "Capturing GPS while the screen is off.",
        channelName: "Race tracking",
        priority: BackgroundGeolocation.NotificationPriority.Low,
        sticky: true,
      },
    },

    // ── Logging ──────────────────────────────────────────────────────
    // Keep the on-device log small; raise to LogLevel.Verbose while
    // debugging a failed screen-locked run.
    logger: {
      logLevel: BackgroundGeolocation.LogLevel.Warning,
    },
  });

  await BackgroundGeolocation.start();

  return {
    stop: async () => {
      try {
        await BackgroundGeolocation.stop();
      } finally {
        locationSub.remove();
      }
    },
  };
}

/**
 * Prompt the user to exempt SailLine from battery optimization. Aggressive
 * OEMs (Xiaomi/OnePlus/some Samsung) throttle the foreground service
 * otherwise, which shows up as multi-minute gaps in the screen-locked
 * test. Safe no-op on iOS and on devices that don't expose the toggle.
 */
export async function requestBatteryOptimizationExemption(): Promise<void> {
  try {
    const isIgnoring =
      await BackgroundGeolocation.deviceSettings.isIgnoringBatteryOptimizations();
    if (!isIgnoring) {
      const request =
        await BackgroundGeolocation.deviceSettings.showIgnoreBatteryOptimizations();
      if (request.seen === false) {
        await BackgroundGeolocation.deviceSettings.show(request);
      }
    }
  } catch {
    // Not all devices/platforms expose this; ignore.
  }
}
