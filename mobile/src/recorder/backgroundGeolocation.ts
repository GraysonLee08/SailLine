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

import BackgroundGeolocation, {
  Location,
  Subscription,
} from "react-native-background-geolocation";

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
 * Transistorsoft reports speed/heading as -1 (and accuracy can be a large
 * sentinel) when a value isn't available; we map those to null so the
 * wire layer emits null rather than a bogus number. speed is m/s,
 * heading is degrees true, accuracy is meters, timestamp is ISO 8601.
 *
 * Exported for unit testing.
 */
export function normalizeLocation(location: Location): LocalPoint {
  const { coords } = location;
  const speed = coords.speed;
  const heading = coords.heading;
  const accuracy = coords.accuracy;
  return {
    recorded_at: location.timestamp,
    lat: coords.latitude,
    lon: coords.longitude,
    speed_kts:
      Number.isFinite(speed) && speed >= 0 ? speed * MS_TO_KTS : null,
    heading_deg:
      Number.isFinite(heading) && heading >= 0 ? heading : null,
    gps_acc_m: Number.isFinite(accuracy) && accuracy >= 0 ? accuracy : null,
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
    // ── Accuracy & cadence ───────────────────────────────────────────
    desiredAccuracy: BackgroundGeolocation.DESIRED_ACCURACY_NAVIGATION,
    distanceFilter: 0,
    locationUpdateInterval: 1000,
    fastestLocationUpdateInterval: 1000,
    disableElasticity: true,

    // ── Lifecycle ────────────────────────────────────────────────────
    // We start/stop explicitly around a race; don't auto-resume on boot
    // or keep running after the app is terminated in Phase 1.
    stopOnTerminate: true,
    startOnBoot: false,
    // We control start() ourselves below, so don't auto-start on ready.
    autoStart: false,

    // ── Android foreground service + notification (Phase 1 plan §3) ──
    foregroundService: true,
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
      priority: BackgroundGeolocation.NOTIFICATION_PRIORITY_LOW,
      sticky: true,
    },

    // ── iOS (build-time modes are set by the config plugin) ──────────
    locationAuthorizationRequest: "Always",
    pausesLocationUpdatesAutomatically: false,
    showsBackgroundLocationIndicator: true,

    // ── Logging ──────────────────────────────────────────────────────
    // Keep the on-device log small; raise to VERBOSE while debugging a
    // failed screen-locked run.
    logLevel: BackgroundGeolocation.LOG_LEVEL_WARNING,
    reset: true,
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
