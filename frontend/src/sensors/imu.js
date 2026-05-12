// IMU sensor module — platform-detected accelerometer + gyroscope streaming.
//
// Two platform paths share one output API:
//   - iOS:     DeviceMotionEvent (requires user-gesture permission on iOS 13+)
//   - Android: Generic Sensor API (Accelerometer + Gyroscope)
//
// The sampler emits combined frames at a fixed cadence (default 10 Hz) by
// reading the latest cached accel + gyro values on each tick. Underlying
// sensors may fire faster (iOS often ~60 Hz); we downsample by latest-value
// rather than averaging, which keeps the gyro/accel phase-aligned within
// one sensor period — good enough for filter ingestion.
//
// Output frame: { t, ax, ay, az, gx, gy, gz } — phone frame, NOT boat frame.
// Accel in m/s², gyro in rad/s. Axis remapping to boat frame is the caller's
// responsibility (handled later by per-race calibration + mount orientation).
//
// Secure-context requirement: both iOS DeviceMotion (iOS 13+) and Android
// Generic Sensor API require HTTPS. localhost counts as secure. For LAN
// testing on a real phone you'll need either HTTPS dev server or a tunnel.

const RAD_PER_DEG = Math.PI / 180;

// ─── Platform detection ──────────────────────────────────────────────

export function detectPlatform() {
    if (typeof window === "undefined") return null;

    // iOS 13+ exposes DeviceMotionEvent.requestPermission. iOS < 13 has
    // DeviceMotionEvent without the permission gate. Either way, if the
    // global is present and Generic Sensor API isn't, take the iOS path.
    const hasDeviceMotion = typeof window.DeviceMotionEvent !== "undefined";
    const hasGenericSensor =
        typeof window.Accelerometer !== "undefined" &&
        typeof window.Gyroscope !== "undefined";

    if (hasGenericSensor) return "generic-sensor";
    if (hasDeviceMotion) return "device-motion";
    return null;
}

export function needsPermissionPrompt() {
    // Only iOS 13+ requires an explicit user-gesture-bound permission call.
    return (
        typeof window !== "undefined" &&
        typeof window.DeviceMotionEvent !== "undefined" &&
        typeof window.DeviceMotionEvent.requestPermission === "function"
    );
}

// ─── Permission acquisition ──────────────────────────────────────────

/**
 * Request sensor permission. MUST be called from a user gesture handler
 * on iOS (button click, touchend, etc.) or the browser will reject it.
 *
 * Returns one of:
 *   - "granted"     — proceed to start()
 *   - "denied"      — user said no; offer them a way to retry
 *   - "not-needed"  — Android or older iOS; just start()
 *   - "unsupported" — no IMU APIs available at all
 */
export async function requestPermission() {
    const platform = detectPlatform();
    if (!platform) return "unsupported";
    if (!needsPermissionPrompt()) return "not-needed";

    try {
        const result = await window.DeviceMotionEvent.requestPermission();
        return result === "granted" ? "granted" : "denied";
    } catch (err) {
        // Most common cause: not called from a user gesture.
        return "denied";
    }
}

// ─── Sampler ─────────────────────────────────────────────────────────

/**
 * Start streaming combined IMU samples at rateHz.
 *
 * Returns a handle: { stop(), platform }. Call stop() to release the
 * underlying sensors and the interval timer.
 *
 * @param {object} opts
 * @param {number} [opts.rateHz=10]   — emission frequency in Hz
 * @param {function} opts.onSample    — called with each combined sample
 * @param {function} [opts.onError]   — called with Error on sensor failure
 */
export function start({ rateHz = 10, onSample, onError }) {
    const platform = detectPlatform();
    if (!platform) {
        onError?.(new Error("No supported IMU API on this device"));
        return { stop() { }, platform: null };
    }

    let latestAccel = null; // { ax, ay, az } m/s²
    let latestGyro = null; // { gx, gy, gz } rad/s
    let cleanupSensors = () => { };

    if (platform === "device-motion") {
        // iOS path. DeviceMotionEvent fires at the device's native rate
        // (typically 60 Hz on iPhone). We just cache the latest and let
        // the 10 Hz sampler tick downsample.
        const handler = (e) => {
            const acc = e.accelerationIncludingGravity;
            const rot = e.rotationRate;
            if (acc && acc.x !== null) {
                latestAccel = { ax: acc.x, ay: acc.y, az: acc.z };
            }
            if (rot && rot.beta !== null) {
                // W3C spec: alpha=z, beta=x, gamma=y rotation rate in deg/s.
                // Convert to rad/s and remap to gx/gy/gz (about phone x/y/z).
                latestGyro = {
                    gx: (rot.beta || 0) * RAD_PER_DEG,
                    gy: (rot.gamma || 0) * RAD_PER_DEG,
                    gz: (rot.alpha || 0) * RAD_PER_DEG,
                };
            }
        };
        window.addEventListener("devicemotion", handler);
        cleanupSensors = () =>
            window.removeEventListener("devicemotion", handler);
    } else {
        // Android Generic Sensor API. Request 2× target rate so each sampler
        // tick has a fresh value to read.
        let accelSensor, gyroSensor;
        try {
            accelSensor = new window.Accelerometer({ frequency: rateHz * 2 });
            gyroSensor = new window.Gyroscope({ frequency: rateHz * 2 });
        } catch (err) {
            onError?.(err);
            return { stop() { }, platform };
        }

        const onAccelRead = () => {
            latestAccel = { ax: accelSensor.x, ay: accelSensor.y, az: accelSensor.z };
        };
        const onGyroRead = () => {
            latestGyro = { gx: gyroSensor.x, gy: gyroSensor.y, gz: gyroSensor.z };
        };
        const onAccelError = (e) => onError?.(e.error || new Error("accel error"));
        const onGyroError = (e) => onError?.(e.error || new Error("gyro error"));

        accelSensor.addEventListener("reading", onAccelRead);
        accelSensor.addEventListener("error", onAccelError);
        gyroSensor.addEventListener("reading", onGyroRead);
        gyroSensor.addEventListener("error", onGyroError);

        try {
            accelSensor.start();
            gyroSensor.start();
        } catch (err) {
            onError?.(err);
            return { stop() { }, platform };
        }

        cleanupSensors = () => {
            try { accelSensor.stop(); } catch { }
            try { gyroSensor.stop(); } catch { }
        };
    }

    // Sampler tick. Emits a combined frame whenever both accel and gyro
    // have produced at least one value. Waits silently otherwise.
    const intervalMs = 1000 / rateHz;
    const intervalId = setInterval(() => {
        if (!latestAccel || !latestGyro) return;
        onSample?.({
            t: performance.now() / 1000,
            ...latestAccel,
            ...latestGyro,
        });
    }, intervalMs);

    return {
        platform,
        stop() {
            clearInterval(intervalId);
            cleanupSensors();
        },
    };
}

// ─── Client-side complementary filter (for gauge display) ────────────

/**
 * Tiny complementary filter for the local heel gauge. The server-side
 * Kalman remains authoritative for advisor logic; this exists so the
 * spirit-level UI doesn't depend on WS round-trip latency.
 *
 * Math: θ_new = α·(θ + ω·dt) + (1-α)·θ_meas
 *   α=0.98 means the gyro dominates over short horizons (snappy
 *   response) and the accel anchors over long horizons (no drift).
 *
 * Inputs use the same phone-frame convention as the sampler.
 */
export function createComplementaryFilter({ alpha = 0.98 } = {}) {
    let heel = 0; // rad
    let pitch = 0; // rad
    let lastT = null;

    return {
        step(sample) {
            const { t, ax, ay, az, gx, gy } = sample;
            const heelMeas = Math.atan2(ax, az);
            const pitchMeas = Math.atan2(-ay, Math.sqrt(ax * ax + az * az));

            if (lastT === null) {
                heel = heelMeas;
                pitch = pitchMeas;
                lastT = t;
            } else {
                const dt = t - lastT;
                lastT = t;
                // Skip pathological dt — keeps the filter sane after a long gap.
                if (dt > 0 && dt < 1.0) {
                    heel = alpha * (heel + gy * dt) + (1 - alpha) * heelMeas;
                    pitch = alpha * (pitch + gx * dt) + (1 - alpha) * pitchMeas;
                }
            }

            return {
                heelDeg: (heel * 180) / Math.PI,
                pitchDeg: (pitch * 180) / Math.PI,
            };
        },
        reset() {
            heel = 0;
            pitch = 0;
            lastT = null;
        },
    };
}