// backgroundGeolocation.ts — Transistorsoft adapter for the RN recorder.
//
// The RN analogue of frontend/src/lib/geolocation.js: it owns ALL shape
// translation between react-native-background-geolocation and the
// recorder's canonical local point shape, plus the start/stop lifecycle
// and the Android foreground-service notification copy.
//
// Two recorder modes (Phase 4 — 2026-06-01):
//
//   * "js" mode (the original Phase-1 design). Transistorsoft is the
//     CAPTURE ENGINE ONLY; the recorder hook owns queueing, batching,
//     flushing, and drop-on-ack. Failure mode demonstrated by the
//     2026-05-31 race: JS-driven flushes don't run when the phone is
//     locked, so uploads stall silently for as long as the user keeps
//     the screen off.
//
//   * "native" mode (Phase 4, flag-gated). Transistorsoft owns the
//     queue (its own SQLite store), batches with maxBatchSize=100, and
//     auto-POSTs to our /telemetry endpoint via a templated body
//     matching the JS uploader's wire shape. Uploads keep flowing
//     even with the screen off and the app fully backgrounded, because
//     the HTTP layer runs in native code. The JS side just observes
//     onHttp events to update LiveStats + the ring buffer.
//
// The original "we don't use Transistorsoft's HTTP layer" design note
// cited two reasons: (a) wire shape mismatch, and (b) hourly-expiring
// Firebase tokens. Phase 4 resolves both — (a) via the locationTemplate
// config below, (b) via the tokenRefresh.ts module which pushes a fresh
// bearer into native config on foreground / token-changed events.
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
 * Phase 4 — optional native-uploader configuration. When provided,
 * Transistorsoft's HTTP layer auto-POSTs to ``url`` with a body of
 * shape ``{ gps: [ {t, lat, lon, sog_kts, cog_deg, gps_acc_m}, ... ] }``
 * matching the JS uploader. Token rotation happens via
 * :func:`setAuthHeader` called from JS on foreground / token-changed
 * events (see tokenRefresh.ts).
 *
 * When omitted the recorder runs in "js" mode and the caller is
 * responsible for flushing via apiFetch.
 */
export type NativeUploaderConfig = {
  /** Full URL including the race id, e.g.
   *  ``https://sailline-api.../api/races/{uuid}/telemetry``. */
  url: string;
  /** Initial Authorization header. Replace later via setAuthHeader. */
  authHeader: string;
};

/**
 * Push a fresh ``Authorization`` header into the running plugin. Safe
 * to call repeatedly. Used by tokenRefresh.ts when the Firebase ID
 * token rotates. No-op (silently) if the plugin isn't initialized
 * yet — the next ready() call will pick up the latest header.
 */
export async function setAuthHeader(authHeader: string): Promise<void> {
  try {
    await BackgroundGeolocation.setConfig({
      http: {
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
      },
    });
  } catch {
    // Plugin not ready yet, or v5 transient — fine. The next ready()
    // picks up whatever was last passed in; missing-token errors will
    // surface as 401s in onHttp, which JS retries against.
  }
}

/**
 * Force a native sync. Useful as the "flush now" user action — drains
 * the local queue immediately rather than waiting on autoSync.
 * Resolves once the sync completes (or rejects on plugin error). Safe
 * to call when no native uploader is configured; becomes a no-op.
 */
export async function syncNow(): Promise<void> {
  try {
    await BackgroundGeolocation.sync();
  } catch {
    // No-op: in JS mode this isn't expected to do anything useful;
    // in native mode a sync failure surfaces as an onHttp event.
  }
}

/**
 * Read the current native queue depth — number of locations awaiting
 * upload. Used by the recorder to reconcile its displayed queueLength
 * against native truth on foreground events. Falls back to 0 on any
 * error so the badge never NaNs out.
 */
export async function getNativeQueueCount(): Promise<number> {
  try {
    return await BackgroundGeolocation.getCount();
  } catch {
    return 0;
  }
}

/**
 * Subscribe to native HTTP events. Returns an unsubscribe handle.
 * The native layer fires this for every POST attempt regardless of
 * whether JS is awake — when JS sleeps the events queue and replay
 * on wake, so the recorder's LiveStats catch up automatically.
 */
export function onNativeHttp(
  callback: (event: {
    success: boolean;
    status: number;
    responseText?: string;
  }) => void,
): { remove: () => void } {
  const sub = BackgroundGeolocation.onHttp((event) => {
    callback({
      success: event.success,
      status: event.status,
      // responseText is present in v5; cast loosely so a future SDK
      // tweak doesn't blow the type-check.
      responseText: (event as { responseText?: string }).responseText,
    });
  });
  return { remove: () => sub.remove() };
}

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
  const point: LocalPoint = {
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

  // 2026-06-03 A4 — sampled diagnostic. The user reports the
  // GuidanceCard often shows "—" for Speed / Track during a race.
  // Two failure modes are plausible: (a) Transistorsoft emits the
  // unavailable-sentinel (-1 in v4, undefined in v5) when stationary
  // and the mapping above correctly drops them to null — visible UI
  // dash is the right behaviour, the bug is purely cosmetic; (b)
  // the SDK never populates speed/heading for some hardware/permission
  // permutation and the values are null even when moving.
  //
  // Log every Nth fix so we can confirm which case is happening from a
  // single short on-water test, without spamming console at 1 Hz. Dev
  // build only — production strips console output via the metro
  // minifier config.
  if (__DEV__) {
    NORMALIZE_LOG_COUNTER += 1;
    if (NORMALIZE_LOG_COUNTER % NORMALIZE_LOG_EVERY === 0) {
      // eslint-disable-next-line no-console
      console.log(
        "[geo] sample speed=%s heading=%s acc=%s → speed_kts=%s heading_deg=%s",
        speed,
        heading,
        accuracy,
        point.speed_kts,
        point.heading_deg,
      );
    }
  }
  return point;
}

// Counter for the sampled normalizeLocation log above. Module-level so
// the cadence is consistent across whatever screen is mounted.
let NORMALIZE_LOG_COUNTER = 0;
const NORMALIZE_LOG_EVERY = 10; // one log per ~10 fixes ≈ once per 10 s

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
  nativeUploader,
}: WatcherCallbacks & {
  nativeUploader?: NativeUploaderConfig;
}): Promise<{ stop: () => Promise<void> }> {
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

    // ── Phase 4: native HTTP uploader (flag-gated by caller) ────────
    //
    // When ``nativeUploader`` is provided, Transistorsoft auto-POSTs
    // every queued location as it arrives, in batches up to 100. The
    // body shape MUST exactly match the JS uploader so the same
    // /telemetry handler accepts both code paths.
    //
    // Config split (v5): `locationTemplate` is a TOP-LEVEL Config
    // option, NOT a member of the http block (that's the v4 shape).
    // It shapes the per-location JSON the plugin emits, independent of
    // whether that JSON is POSTed via the http layer or returned via
    // the JS onLocation callback. The http block carries the transport
    // (url, headers, batching, rootProperty).
    //
    // locationTemplate uses EJS-style interpolation; the supported
    // variables are the keys of the plugin's Location object. We map
    // them to our wire shape, including the m/s → knots conversion
    // for speed. Negative sentinels (when GPS hasn't computed a value)
    // are passed through as-is; the backend's pydantic pre-validator
    // (Phase 4) coerces negative values to null so one sentinel
    // sample can't 422 the whole batch.
    //
    // rootProperty="gps" wraps the array as { "gps": [...] } matching
    // TelemetryBatch on the server. (v4 called this `httpRootProperty`.)
    ...(nativeUploader
      ? {
          // Top-level: shapes the per-location JSON. Numbers emitted
          // UNQUOTED so they land as JSON numbers, not strings.
          locationTemplate:
            '{"t":"<%= timestamp %>",' +
            '"lat":<%= latitude %>,' +
            '"lon":<%= longitude %>,' +
            '"sog_kts":<%= speed * 1.943844 %>,' +
            '"cog_deg":<%= heading %>,' +
            '"gps_acc_m":<%= accuracy %>}',
          http: {
            url: nativeUploader.url,
            method: "POST",
            autoSync: true,
            autoSyncThreshold: 1,
            batchSync: true,
            maxBatchSize: 100,
            rootProperty: "gps",
            headers: {
              Authorization: nativeUploader.authHeader,
              "Content-Type": "application/json",
            },
          },
        }
      : {}),
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
