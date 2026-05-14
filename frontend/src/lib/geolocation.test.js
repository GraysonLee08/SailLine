// frontend/src/lib/geolocation.test.js
//
// Tests for the platform-adaptive geolocation adapter.
//
// Scope: web path + pure functions only.
//   - normalizePosition (both shapes, null handling)
//   - isNative() detection via window.Capacitor
//   - createWatcher web branch: option merge, point normalisation,
//     error flow, stop()
//
// Out of scope: the native (Capacitor) branch. The plugin is
// dynamic-imported at runtime, lives only in the native build, and
// isn't installed in this jsdom test environment. The native branch
// is exercised by the on-device smoke test documented in the session
// summary.

import {
  describe,
  it,
  expect,
  vi,
  beforeEach,
  afterEach,
} from "vitest";
import {
  isNative,
  normalizePosition,
  createWatcher,
  DEFAULT_WATCH_OPTIONS,
} from "./geolocation";

// ─── Helpers ────────────────────────────────────────────────────────

function makeWebPos({
  lat = 42.1,
  lon = -87.5,
  speed = 5.144, // m/s ≈ 10 kt
  heading = 270,
  timestamp = Date.parse("2026-05-13T18:00:00Z"),
} = {}) {
  return {
    coords: {
      latitude: lat,
      longitude: lon,
      speed,
      heading,
      accuracy: 5,
      altitude: 0,
      altitudeAccuracy: 5,
    },
    timestamp,
  };
}

function makeNativePos({
  lat = 42.1,
  lon = -87.5,
  speed = 5.144,
  bearing = 270,
  time = Date.parse("2026-05-13T18:00:00Z"),
} = {}) {
  return {
    latitude: lat,
    longitude: lon,
    speed,
    bearing,
    time,
    accuracy: 5,
    altitude: 0,
    altitudeAccuracy: 5,
    simulated: false,
  };
}

// ─── normalizePosition ──────────────────────────────────────────────

describe("normalizePosition", () => {
  it("converts a web GeolocationPosition to the canonical shape", () => {
    const point = normalizePosition(makeWebPos(), "web");
    expect(point).toEqual({
      recorded_at: "2026-05-13T18:00:00.000Z",
      lat: 42.1,
      lon: -87.5,
      speed_kts: 5.144 * 1.943844,
      heading_deg: 270,
    });
  });

  it("converts a native Capacitor Location to the canonical shape", () => {
    const point = normalizePosition(makeNativePos(), "native");
    expect(point).toEqual({
      recorded_at: "2026-05-13T18:00:00.000Z",
      lat: 42.1,
      lon: -87.5,
      speed_kts: 5.144 * 1.943844,
      heading_deg: 270,
    });
  });

  it("passes null speed/heading through as null (web)", () => {
    const point = normalizePosition(
      makeWebPos({ speed: null, heading: null }),
      "web",
    );
    expect(point.speed_kts).toBeNull();
    expect(point.heading_deg).toBeNull();
  });

  it("passes null speed/bearing through as null (native)", () => {
    const point = normalizePosition(
      makeNativePos({ speed: null, bearing: null }),
      "native",
    );
    expect(point.speed_kts).toBeNull();
    expect(point.heading_deg).toBeNull();
  });

  it("falls back to Date.now() when native time is missing", () => {
    // Note: destructuring defaults in makeNativePos fire on `undefined`,
    // so passing `time: undefined` would still produce a fixed timestamp.
    // We pass `null` — Number.isFinite(null) is false, which is what
    // normalizePosition checks before falling back.
    const before = Date.now();
    const point = normalizePosition(makeNativePos({ time: null }), "native");
    const after = Date.now();
    const ts = Date.parse(point.recorded_at);
    expect(ts).toBeGreaterThanOrEqual(before);
    expect(ts).toBeLessThanOrEqual(after);
  });
});

// ─── isNative ───────────────────────────────────────────────────────

describe("isNative", () => {
  const origCap = window.Capacitor;

  afterEach(() => {
    if (origCap === undefined) {
      delete window.Capacitor;
    } else {
      window.Capacitor = origCap;
    }
  });

  it("returns false when window.Capacitor is absent", () => {
    delete window.Capacitor;
    expect(isNative()).toBe(false);
  });

  it("returns false when isNativePlatform() returns false", () => {
    window.Capacitor = { isNativePlatform: () => false };
    expect(isNative()).toBe(false);
  });

  it("returns true when isNativePlatform() returns true", () => {
    window.Capacitor = { isNativePlatform: () => true };
    expect(isNative()).toBe(true);
  });

  it("returns false when isNativePlatform throws", () => {
    window.Capacitor = {
      isNativePlatform: () => {
        throw new Error("boom");
      },
    };
    expect(isNative()).toBe(false);
  });
});

// ─── createWatcher (web branch) ─────────────────────────────────────

describe("createWatcher — web branch", () => {
  const origGeolocation = navigator.geolocation;
  let watchPosition;
  let clearWatch;
  let nextWatchId;
  let calls;

  beforeEach(() => {
    nextWatchId = 1;
    calls = [];
    watchPosition = vi.fn((success, errorCb, opts) => {
      const id = nextWatchId++;
      calls.push({ id, success, errorCb, opts });
      return id;
    });
    clearWatch = vi.fn();
    Object.defineProperty(navigator, "geolocation", {
      configurable: true,
      value: { watchPosition, clearWatch },
    });
    // Ensure native detection short-circuits to web.
    delete window.Capacitor;
  });

  afterEach(() => {
    Object.defineProperty(navigator, "geolocation", {
      configurable: true,
      value: origGeolocation,
    });
  });

  it("calls watchPosition with the default options merged with overrides", async () => {
    await createWatcher({
      onPosition: () => {},
      options: { timeout: 5000 },
    });
    expect(watchPosition).toHaveBeenCalledOnce();
    const opts = calls[0].opts;
    expect(opts).toEqual({
      enableHighAccuracy: DEFAULT_WATCH_OPTIONS.enableHighAccuracy,
      timeout: 5000,
      maximumAge: DEFAULT_WATCH_OPTIONS.maximumAge,
    });
  });

  it("delivers normalised points to onPosition", async () => {
    const received = [];
    await createWatcher({ onPosition: (p) => received.push(p) });
    // Simulate one fix.
    calls[0].success(makeWebPos());
    expect(received).toHaveLength(1);
    expect(received[0]).toMatchObject({
      lat: 42.1,
      lon: -87.5,
      heading_deg: 270,
    });
    expect(received[0].speed_kts).toBeCloseTo(10, 1);
  });

  it("routes geolocation errors to onError", async () => {
    const errors = [];
    await createWatcher({
      onPosition: () => {},
      onError: (e) => errors.push(e),
    });
    const fakeErr = { code: 1, message: "User denied geolocation" };
    calls[0].errorCb(fakeErr);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toBe(fakeErr);
  });

  it("stop() clears the active watch and is idempotent", async () => {
    const handle = await createWatcher({ onPosition: () => {} });
    await handle.stop();
    expect(clearWatch).toHaveBeenCalledWith(calls[0].id);
    // Second stop is a no-op — must not throw.
    await handle.stop();
    expect(clearWatch).toHaveBeenCalledTimes(1);
  });

  it("calls onError and returns a no-op handle when geolocation is missing", async () => {
    Object.defineProperty(navigator, "geolocation", {
      configurable: true,
      value: undefined,
    });
    const errors = [];
    const handle = await createWatcher({
      onPosition: () => {},
      onError: (e) => errors.push(e),
    });
    expect(errors).toHaveLength(1);
    expect(errors[0].message).toMatch(/not supported/i);
    // No throw on stop.
    await expect(handle.stop()).resolves.toBeUndefined();
  });

  it("throws if onPosition is not a function", async () => {
    await expect(createWatcher({})).rejects.toThrow(TypeError);
  });
});
