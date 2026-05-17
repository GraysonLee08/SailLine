// orientation.js — wrapper around `window.DeviceOrientationEvent` that
// gives the recorder one cached W3C reading on demand.
//
// Why a separate module from `sensors/imu.js`:
//
//   imu.js streams raw accel+gyro and runs a complementary filter — the
//   `?debug=sensors` view and any future live filter logic uses that.
//   For race recording we only need absolute Euler angles (heel, pitch,
//   yaw), which DeviceOrientationEvent delivers natively without a
//   filter. Two modules keep the responsibilities distinct.
//
// Permission model:
//
//   iOS 13+ requires `DeviceOrientationEvent.requestPermission()` from a
//   user gesture (button click). This module owns the permission flow.
//   Android Chrome grants access automatically over HTTPS / localhost.
//   Older iOS, desktop browsers, and devices without orientation sensors
//   return "unsupported" — callers should treat that as "skip the IMU
//   path, GPS-only recording still works."
//
// Output:
//
//   `latest()` returns the most recent `{alpha, beta, gamma}` cached by
//   the listener, or null if no event has fired yet (or if the sensors
//   haven't produced valid values). Polling rather than callbacks keeps
//   the recorder code simple — it ticks on its own 10 Hz timer.
//
// HTTPS requirement: DeviceOrientationEvent on iOS *requires* a secure
// context (HTTPS or localhost). `npm run dev` over LAN to a real phone
// needs the basic-ssl Vite plugin (already configured in this project).

const _state = {
  alpha: null,
  beta: null,
  gamma: null,
  receivedAny: false,
};

let _attached = false;

function _handler(event) {
  // The first one or two events on iOS can deliver null for some or all
  // fields. We update only the fields that arrived with a finite value
  // so a partial event doesn't erase a previously-good reading.
  if (typeof event.alpha === "number" && Number.isFinite(event.alpha)) {
    _state.alpha = event.alpha;
  }
  if (typeof event.beta === "number" && Number.isFinite(event.beta)) {
    _state.beta = event.beta;
  }
  if (typeof event.gamma === "number" && Number.isFinite(event.gamma)) {
    _state.gamma = event.gamma;
  }
  _state.receivedAny = true;
}

/**
 * True iff the browser exposes the DeviceOrientationEvent global.
 * Coarse signal — does not guarantee the device actually has sensors.
 */
export function isSupported() {
  return (
    typeof window !== "undefined" &&
    typeof window.DeviceOrientationEvent !== "undefined"
  );
}

/**
 * True iff this platform requires an iOS-style permission prompt.
 * Implies we MUST call `requestPermission()` from a user gesture before
 * `start()` will receive any events.
 */
export function needsPermissionPrompt() {
  return (
    isSupported() &&
    typeof window.DeviceOrientationEvent.requestPermission === "function"
  );
}

/**
 * Request orientation permission. Call from a user-gesture handler on
 * iOS or the browser will silently reject. Returns one of:
 *
 *   "granted"     — proceed to start()
 *   "denied"      — user said no; recorder records GPS-only
 *   "not-needed"  — Android or older iOS; just start()
 *   "unsupported" — no DeviceOrientationEvent at all
 */
export async function requestPermission() {
  if (!isSupported()) return "unsupported";
  if (!needsPermissionPrompt()) return "not-needed";

  try {
    const result = await window.DeviceOrientationEvent.requestPermission();
    return result === "granted" ? "granted" : "denied";
  } catch {
    // Most common cause: not called from a user gesture. Treat as denied.
    return "denied";
  }
}

/**
 * Attach the orientation listener. Idempotent — repeated calls are a
 * no-op, and the resulting `stop` may be called any number of times.
 *
 * Returns `{ stop }`. The recorder polls `latest()` directly rather
 * than listening for events, so there's no onSample callback here.
 */
export function start() {
  if (!isSupported()) {
    return { stop() {} };
  }
  if (!_attached) {
    try {
      window.addEventListener("deviceorientation", _handler, { passive: true });
      _attached = true;
    } catch {
      // Some sandboxed environments (older WebViews) throw on addListener;
      // surface as a no-op handle.
      return { stop() {} };
    }
  }
  return {
    stop() {
      if (!_attached) return;
      try {
        window.removeEventListener("deviceorientation", _handler);
      } catch {
        /* best effort */
      }
      _attached = false;
      _state.alpha = null;
      _state.beta = null;
      _state.gamma = null;
      _state.receivedAny = false;
    },
  };
}

/**
 * Return the latest cached orientation reading, or null if nothing has
 * arrived yet. The caller is responsible for axis-remapping into the
 * boat frame (see `lib/imuAxes.js`).
 */
export function latest() {
  if (!_state.receivedAny) return null;
  const { alpha, beta, gamma } = _state;
  // beta and gamma are required; alpha can briefly be null while the
  // magnetometer is acquiring. Caller decides what to do with null yaw.
  if (
    typeof beta !== "number" ||
    !Number.isFinite(beta) ||
    typeof gamma !== "number" ||
    !Number.isFinite(gamma)
  ) {
    return null;
  }
  return { alpha, beta, gamma };
}

/**
 * Test helper — reset internal state. Not exported on the public
 * surface that callers should use, but harmless if invoked.
 */
export function _resetForTests() {
  _state.alpha = null;
  _state.beta = null;
  _state.gamma = null;
  _state.receivedAny = false;
  _attached = false;
}
