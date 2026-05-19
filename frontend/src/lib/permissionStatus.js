// permissionStatus.js — platform-adaptive Location-permission probe.
//
// Mirrors the structure of `geolocation.js` (which decides where GPS
// fixes come from): a thin layer that hides the difference between the
// web Permissions API and Capacitor's background-geolocation plugin.
//
// Why this matters: GPS can keep working *long enough to look fine* even
// when the OS has silently downgraded the permission. The classic case
// on Android is "Allow all the time" → "While using the app": fore-
// ground fixes continue, the watcher doesn't throw, but the moment the
// screen locks the foreground service is killed and the recorder stops
// receiving fixes. The user only notices when they get home and look at
// the breadcrumb. The banner consumes this module to surface the
// downgrade before the race, not after.
//
// Surface:
//
//   getLocationPermission(): Promise<Status>
//     Returns the current snapshot. Status shape:
//       {
//         state:      "granted" | "prompt" | "denied" | "unknown" | "unsupported",
//         background: boolean | null   // null when concept doesn't apply (web)
//                                      // false  → granted but only when in use
//                                      // true   → granted including background
//         source:     "web-permissions-api" | "capacitor-plugin" |
//                     "fallback" | "unsupported"
//       }
//
//   subscribeLocationPermission(callback): unsubscribe
//     Fires immediately with the current snapshot, then again on every
//     change. On the web that's the native Permissions API
//     `onchange` event; in Capacitor we poll on visibilitychange + at a
//     conservative interval, since the plugin doesn't expose a change
//     event.
//
// Like `geolocation.js`, we deliberately access Capacitor through the
// `window.Capacitor.Plugins` global registry rather than importing any
// `@capacitor/*` module. That keeps the web build compiling cleanly when
// the native deps aren't installed — the same trap that surfaced in
// the Capacitor adapter session (see docs/2026-05-14-session-summary-capacitor.md).

const POLL_MS = 15_000; // native fallback poll cadence

function isNativeCapacitor() {
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
 * Web path: `navigator.permissions.query({name: 'geolocation'})`.
 *
 * Returns the wrapped status object. Falls back to "unsupported" when
 * the API is missing (older Safari, some embedded webviews) — the UI
 * shouldn't show a downgrade warning when we can't tell.
 */
async function queryWebPermission() {
  if (
    typeof navigator === "undefined" ||
    !navigator.permissions ||
    typeof navigator.permissions.query !== "function"
  ) {
    return {
      state: "unsupported",
      background: null,
      source: "unsupported",
    };
  }
  try {
    const result = await navigator.permissions.query({ name: "geolocation" });
    return {
      state: result.state, // "granted" | "prompt" | "denied"
      background: null,    // web has no separate background concept
      source: "web-permissions-api",
      _raw: result,        // exposed for subscribe-side onchange wiring
    };
  } catch {
    return {
      state: "unsupported",
      background: null,
      source: "unsupported",
    };
  }
}

/**
 * Native path: ask the @capacitor-community/background-geolocation
 * plugin (or whatever else is registered) for its permission state.
 *
 * The plugin exposes `checkPermissions()` returning
 * `{location: 'granted'|'denied'|'prompt', backgroundLocation: same}`.
 * We tolerate older versions that don't have it by gracefully reporting
 * `unsupported`.
 */
async function queryNativePermission() {
  const plugin =
    typeof window !== "undefined" &&
    window?.Capacitor?.Plugins?.BackgroundGeolocation;
  if (!plugin || typeof plugin.checkPermissions !== "function") {
    return {
      state: "unsupported",
      background: null,
      source: "unsupported",
    };
  }
  try {
    const raw = await plugin.checkPermissions();
    // Be lenient about shape — different plugin versions name the
    // fields slightly differently (location / locationAlways / etc.).
    const loc =
      raw?.location ??
      raw?.foreground ??
      raw?.locationForeground ??
      "unknown";
    const bg =
      raw?.backgroundLocation ??
      raw?.background ??
      raw?.locationAlways ??
      null;
    const state =
      loc === "granted" ? "granted"
        : loc === "denied" ? "denied"
        : loc === "prompt" ? "prompt"
        : "unknown";
    // Background is meaningful only when foreground is granted.
    let background = null;
    if (state === "granted" && bg != null) {
      background = bg === "granted";
    }
    return {
      state,
      background,
      source: "capacitor-plugin",
    };
  } catch {
    return {
      state: "unknown",
      background: null,
      source: "capacitor-plugin",
    };
  }
}

/**
 * One-shot snapshot of the current Location permission state.
 */
export async function getLocationPermission() {
  if (isNativeCapacitor()) {
    return queryNativePermission();
  }
  return queryWebPermission();
}

/**
 * Subscribe to permission state. Fires once immediately with the
 * current snapshot, then again on every transition the platform tells
 * us about. Returns an unsubscribe function.
 *
 * Web: piggybacks on PermissionStatus.onchange.
 * Native: revalidates on visibilitychange (when the user comes back
 *         from Settings) plus a 15 s safety poll. We do not assume the
 *         plugin emits a change event.
 */
export function subscribeLocationPermission(callback) {
  if (typeof callback !== "function") {
    return () => undefined;
  }
  let stopped = false;
  let permRef = null;        // for the web onchange listener removal
  let pollId = null;
  let onVisible = null;

  // Push the current snapshot out immediately so the consumer doesn't
  // render a "...checking" state for half a second.
  (async () => {
    const snap = await getLocationPermission();
    if (stopped) return;
    if (snap.source === "web-permissions-api" && snap._raw) {
      permRef = snap._raw;
      const handler = () => {
        if (stopped) return;
        callback({
          state: permRef.state,
          background: null,
          source: "web-permissions-api",
        });
      };
      try {
        permRef.addEventListener("change", handler);
      } catch {
        // Older browsers expose `onchange` only.
        permRef.onchange = handler;
      }
      // Stash for cleanup.
      permRef.__sailLineHandler = handler;
    }
    // Strip the internal _raw before handing to user code.
    const { _raw: _omit, ...userFacing } = snap;
    callback(userFacing);
  })();

  if (isNativeCapacitor()) {
    // Re-query when the page becomes visible (user just came back from
    // the system Settings app).
    onVisible = async () => {
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        const snap = await getLocationPermission();
        if (!stopped) callback(snap);
      }
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisible);
    }
    // Safety poll — slow enough not to thrash, fast enough that a
    // background-OS-level permission flip is caught within a race.
    pollId = setInterval(async () => {
      const snap = await getLocationPermission();
      if (!stopped) callback(snap);
    }, POLL_MS);
  }

  return function unsubscribe() {
    stopped = true;
    if (permRef && permRef.__sailLineHandler) {
      try {
        permRef.removeEventListener("change", permRef.__sailLineHandler);
      } catch {
        permRef.onchange = null;
      }
      permRef.__sailLineHandler = null;
    }
    if (pollId) {
      clearInterval(pollId);
      pollId = null;
    }
    if (onVisible && typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisible);
      onVisible = null;
    }
  };
}

/**
 * Classify a status into one of:
 *   "ok"         — no banner needed
 *   "denied"     — permission was revoked / blocked outright
 *   "background" — granted in foreground only; will pause when locked
 *   "unknown"    — couldn't tell; don't warn (avoids false positives)
 *
 * Exported so the banner and tests share one source of truth.
 */
export function classifyStatus(status) {
  if (!status) return "unknown";
  if (status.state === "denied") return "denied";
  if (status.state === "granted") {
    // Background is only meaningful on native. `null` means we don't
    // know / don't care, which is the web case.
    if (status.background === false) return "background";
    return "ok";
  }
  if (status.state === "prompt") {
    // Not granted yet, but also not denied. Treat as denied for the
    // recording-warning banner — recording without permission is the
    // same broken state from the user's perspective.
    return "denied";
  }
  return "unknown";
}
