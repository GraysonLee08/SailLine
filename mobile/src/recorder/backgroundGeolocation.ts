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
 * The full http block of the CURRENT native-uploader session, captured
 * by startWatcher. setAuthHeader re-sends the whole block (not just
 * headers) on every token rotation.
 *
 * WHY (2026-07-07 — the zero-upload bug): setConfig({http: {headers}})
 * passes a PARTIAL http group across the bridge, and the native
 * TSConfig treats the group as a unit — the partial update wiped
 * ``http.url`` seconds after start (the onIdTokenChanged listener
 * fires almost immediately after start() fetches its token). With no
 * url, Transistorsoft persists nothing and syncs nothing: 543 captured
 * / 0 uploaded on the 2026-07-07 Test Walk. Re-sending the complete
 * block makes token rotation safe regardless of the native layer's
 * merge semantics.
 */
let lastNativeHttpConfig: {
  url: string;
  method: "POST";
  autoSync: boolean;
  autoSyncThreshold: number;
  batchSync: boolean;
  maxBatchSize: number;
  rootProperty: string;
} | null = null;

/**
 * Push a fresh ``Authorization`` header into the running plugin. Safe
 * to call repeatedly. Used by tokenRefresh.ts when the Firebase ID
 * token rotates. No-op (silently) if the plugin isn't initialized
 * yet — the next ready() call will pick up the latest header.
 *
 * Always re-sends the complete http block (see lastNativeHttpConfig).
 * In js-uploader mode lastNativeHttpConfig is null and this remains
 * the old headers-only push — harmless there, since js mode never
 * configures a native url in the first place.
 */
export async function setAuthHeader(authHeader: string): Promise<void> {
  try {
    await BackgroundGeolocation.setConfig({
      http: {
        ...(lastNativeHttpConfig ?? {}),
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
 * Is the native tracking service currently running? Used by the
 * relaunch reconciler (RecorderContext) to discover a session that
 * survived a JS process kill or a reboot (stopOnTerminate:false /
 * startOnBoot:true, 2026-07-05). Falls back to false on any error —
 * "not tracking" is the safe answer for a reconciler deciding whether
 * to re-attach.
 */
export async function isPluginTracking(): Promise<boolean> {
  try {
    const state = await BackgroundGeolocation.getState();
    return state.enabled === true;
  } catch {
    return false;
  }
}

/**
 * Stop the native service directly, without a watcher handle. The
 * reconciler's teardown path for orphaned sessions (plugin running
 * but its race is already ended/deleted): drain what's queued, then
 * kill the service. Safe to call when already stopped.
 */
export async function stopPlugin(): Promise<void> {
  try {
    await BackgroundGeolocation.stop();
  } catch {
    // Already stopped or plugin not ready — nothing to tear down.
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
 *   - disableStopDetection: true → never auto-pause into the stationary
 *                                  state (see the 2026-06-15 note below)
 *   - changePace(true) post-start → force the MOVING state immediately
 *
 * 2026-06-15 — stationary-state fix (the "recorder ran 81 min, captured
 * ZERO points" on-water failure). Transistorsoft boots into a STATIONARY
 * state and only begins emitting locations once its motion-activity
 * detector decides the device is "moving". Nothing here used to force
 * that transition, so on a sailboat — whose smooth, slow motion the
 * Android activity classifier does not reliably label as moving — the
 * plugin could sit stationary for the whole race and never record a
 * fix (no error: it was "working as configured", just waiting for
 * motion that never registered). distanceFilter:0 /
 * pausesLocationUpdatesAutomatically:false do NOT help — they govern
 * behaviour *while already moving*. The deterministic fix is two-fold:
 *   1. disableStopDetection:true — the plugin never auto-pauses back to
 *      stationary on a low-motion lull (head-to-wind, pre-start drift).
 *   2. changePace(true) right after start() — force the moving state so
 *      capture begins on the first fix instead of on a motion-detector
 *      transition that may never fire.
 * Tradeoff: continuous GPS = higher battery. Acceptable for a bounded,
 * foreground-service race recording, and the behaviour we actually want.
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
  persistSession = false,
}: WatcherCallbacks & {
  nativeUploader?: NativeUploaderConfig;
  /**
   * 2026-07-05 — survive process kill + reboot. When true the config
   * flips to ``stopOnTerminate: false`` + ``startOnBoot: true`` so an
   * OS kill (or the user swiping the app away, or a mid-race reboot)
   * does NOT stop capture: the foreground service keeps running (and
   * the native HTTP layer keeps uploading) with no JS above it. The
   * relaunch reconciler (RecorderContext + activeSession.ts) re-attaches
   * the UI on next launch.
   *
   * Callers should only pass true in native-uploader mode: a revived
   * js-mode service would capture into the void (no JS = no queue, no
   * flush), burning battery for data that never lands.
   */
  persistSession?: boolean;
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

  // Capture the session's full http block BEFORE ready() so both the
  // ready() config below and every subsequent setAuthHeader() token
  // rotation send the identical complete group. Cleared for js-mode
  // sessions so a stale url can't leak into a later header push.
  lastNativeHttpConfig = nativeUploader
    ? {
        url: nativeUploader.url,
        method: "POST",
        autoSync: true,
        autoSyncThreshold: 1,
        batchSync: true,
        maxBatchSize: 100,
        rootProperty: "gps",
      }
    : null;

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
      // 2026-06-15 stationary-state fix (see startWatcher docstring). Keep
      // the plugin out of the motion-detector-gated stationary state for
      // the whole session — a sailboat's low-motion lulls must not pause
      // capture. Paired with changePace(true) after start().
      disableStopDetection: true,
      locationAuthorizationRequest: "Always",
      pausesLocationUpdatesAutomatically: false,
      showsBackgroundLocationIndicator: true,
    },

    // ── App lifecycle + Android foreground service ───────────────────
    // Phase 1 hard-coded stopOnTerminate:true / startOnBoot:false ("we
    // start/stop explicitly around a race"). 2026-07-05: that policy
    // lost a race — the OS killed the app mid-race and recording died
    // with it. persistSession (native-uploader sessions only) now keeps
    // the service alive across kill/reboot; see the startWatcher
    // docstring and activeSession.ts for the reconcile contract.
    app: {
      stopOnTerminate: !persistSession,
      startOnBoot: persistSession,
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
    // 2026-07-06 — TEMPORARILY Verbose to diagnose the heartbeat-only
    // capture bug (native-mode session recorded ~1 fix/min at :30 past
    // each minute — the stationary-state signature — despite
    // disableStopDetection + changePace(true)). The native SDK logs to
    // logcat independent of JS console stripping:
    //   adb logcat -d -s TSLocationManager
    // Revert to LogLevel.Warning once the motion-state issue is fixed.
    logger: {
      logLevel: BackgroundGeolocation.LogLevel.Verbose,
    },

    // ── Phase 4: native HTTP uploader (flag-gated by caller) ────────
    //
    // When ``nativeUploader`` is provided, Transistorsoft auto-POSTs
    // every queued location as it arrives, in batches up to 100. The
    // body shape MUST exactly match the JS uploader so the same
    // /telemetry handler accepts both code paths.
    //
    // Config placement (2026-07-07 fix): in the v5 Config,
    // `locationTemplate` lives in the PERSISTENCE sub-config
    // (PersistenceConfig.d.ts) — this code previously passed it
    // TOP-LEVEL, where the plugin silently ignored it. Metro doesn't
    // type-check, so the violation shipped; `tsc --noEmit` would have
    // caught it.
    //
    // Template quoting (also 2026-07-07): speed/heading/accuracy are
    // OPTIONAL in v5 — `undefined` instead of v4's `-1` sentinel. An
    // unquoted `"cog_deg":<%= heading %>` with heading undefined
    // renders `"cog_deg":` — invalid JSON, killing the whole batch.
    // Quoting those three fields makes a missing value render as ""
    // (valid JSON); the backend pre-validator coerces "" and other
    // non-numeric strings to null, and parses numeric strings. Fields
    // that are ALWAYS present (timestamp, latitude, longitude) stay
    // in their natural types.
    //
    // rootProperty="gps" wraps the array as { "gps": [...] } matching
    // TelemetryBatch on the server. (v4 called this `httpRootProperty`.)
    ...(nativeUploader
      ? {
          persistence: {
            locationTemplate:
              '{"t":"<%= timestamp %>",' +
              '"lat":<%= latitude %>,' +
              '"lon":<%= longitude %>,' +
              '"sog_kts":"<%= speed * 1.943844 %>",' +
              '"cog_deg":"<%= heading %>",' +
              '"gps_acc_m":"<%= accuracy %>"}',
          },
          http: {
            // Same block just captured above; `?? {}` only narrows the
            // type — in this branch it is always non-null.
            ...(lastNativeHttpConfig ?? {}),
            headers: {
              Authorization: nativeUploader.authHeader,
              "Content-Type": "application/json",
            },
          },
        }
      : {}),
  });

  await BackgroundGeolocation.start();

  // 2026-06-15 — force the MOVING state immediately (see the startWatcher
  // docstring). Without this the plugin waits for a motion-activity
  // transition that, on a sailboat, may never fire — the 81-minute
  // zero-point race. Best-effort: tracking has already started by this
  // point, so a transient changePace failure should not tear down an
  // otherwise-live watcher. If it throws, we are no worse off than the
  // pre-fix behaviour; the no-first-fix watchdog (fix 3) is the backstop.
  try {
    await BackgroundGeolocation.changePace(true);
  } catch (e) {
    onError?.(e instanceof Error ? e : new Error(String(e)));
  }

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
