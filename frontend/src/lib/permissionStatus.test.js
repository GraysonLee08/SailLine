// frontend/src/lib/permissionStatus.test.js
//
// Tests for the platform-adaptive Location-permission probe.
//
// Scope: web branch + the platform-agnostic `classifyStatus` helper.
//   - getLocationPermission() with a stubbed navigator.permissions
//   - subscribeLocationPermission() onchange-event wiring
//   - classifyStatus() decision table
//
// Out of scope: the Capacitor branch. We don't install the plugin in
// jsdom; the native path is exercised in on-device smoke tests. The
// branch IS partially covered via the native-detection guard test
// below, which proves we route correctly when `window.Capacitor` is
// present.

import {
  describe,
  it,
  expect,
  vi,
  beforeEach,
  afterEach,
} from "vitest";

import {
  classifyStatus,
  getLocationPermission,
  subscribeLocationPermission,
} from "./permissionStatus";

// ─── Helpers ────────────────────────────────────────────────────────

/**
 * Build a fake PermissionStatus object with controllable state and an
 * addEventListener that captures the handler so the test can fire it.
 */
function makeFakePermissionStatus(initialState) {
  let state = initialState;
  let handler = null;
  return {
    get state() {
      return state;
    },
    set state(v) {
      state = v;
    },
    addEventListener(event, fn) {
      if (event === "change") handler = fn;
    },
    removeEventListener(event, fn) {
      if (event === "change" && handler === fn) handler = null;
    },
    /** Test-only: fire the registered onchange. */
    _fire() {
      if (handler) handler({ target: this });
    },
  };
}

beforeEach(() => {
  // Wipe both potential platforms so each test sets up its own world.
  delete global.window;
  delete global.navigator;
  delete global.document;
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ─── classifyStatus (pure function) ─────────────────────────────────

describe("classifyStatus", () => {
  it("returns 'ok' for granted with background irrelevant or true", () => {
    expect(classifyStatus({ state: "granted", background: null })).toBe("ok");
    expect(classifyStatus({ state: "granted", background: true })).toBe("ok");
  });

  it("returns 'background' for granted-foreground-only", () => {
    expect(
      classifyStatus({ state: "granted", background: false }),
    ).toBe("background");
  });

  it("returns 'denied' for explicit denial", () => {
    expect(classifyStatus({ state: "denied", background: null })).toBe(
      "denied",
    );
  });

  it("treats 'prompt' as denied (recording without permission is broken)", () => {
    expect(classifyStatus({ state: "prompt", background: null })).toBe(
      "denied",
    );
  });

  it("returns 'unknown' for unknown / unsupported / missing input", () => {
    expect(classifyStatus({ state: "unknown" })).toBe("unknown");
    expect(classifyStatus({ state: "unsupported" })).toBe("unknown");
    expect(classifyStatus(null)).toBe("unknown");
    expect(classifyStatus(undefined)).toBe("unknown");
  });
});

// ─── getLocationPermission — web branch ─────────────────────────────

describe("getLocationPermission — web branch", () => {
  it("returns the navigator.permissions.query result", async () => {
    const status = makeFakePermissionStatus("granted");
    global.navigator = {
      permissions: {
        query: vi.fn().mockResolvedValue(status),
      },
    };
    const out = await getLocationPermission();
    expect(navigator.permissions.query).toHaveBeenCalledWith({
      name: "geolocation",
    });
    expect(out.state).toBe("granted");
    expect(out.background).toBeNull();
    expect(out.source).toBe("web-permissions-api");
  });

  it("returns 'unsupported' when navigator.permissions is missing", async () => {
    global.navigator = {};
    const out = await getLocationPermission();
    expect(out.state).toBe("unsupported");
    expect(out.source).toBe("unsupported");
  });

  it("returns 'unsupported' when query throws", async () => {
    global.navigator = {
      permissions: {
        query: vi.fn().mockRejectedValue(new Error("nope")),
      },
    };
    const out = await getLocationPermission();
    expect(out.state).toBe("unsupported");
    expect(out.source).toBe("unsupported");
  });
});

// ─── getLocationPermission — native routing guard ───────────────────

describe("getLocationPermission — native routing guard", () => {
  it("routes to the Capacitor branch when window.Capacitor is native", async () => {
    // No BackgroundGeolocation plugin attached → branch returns
    // 'unsupported'. The test is really proving the routing went
    // through `queryNativePermission`, not the web one — by setting
    // a non-stub navigator.permissions and confirming it was never
    // called.
    const fakeNavQuery = vi.fn().mockResolvedValue(
      makeFakePermissionStatus("granted"),
    );
    global.navigator = {
      permissions: { query: fakeNavQuery },
    };
    global.window = {
      Capacitor: {
        isNativePlatform: () => true,
        Plugins: {}, // no BackgroundGeolocation
      },
    };
    const out = await getLocationPermission();
    expect(fakeNavQuery).not.toHaveBeenCalled();
    expect(out.source).toBe("unsupported");
  });

  it("reads from the plugin's checkPermissions() when available", async () => {
    const checkPermissions = vi.fn().mockResolvedValue({
      location: "granted",
      backgroundLocation: "denied",
    });
    global.window = {
      Capacitor: {
        isNativePlatform: () => true,
        Plugins: { BackgroundGeolocation: { checkPermissions } },
      },
    };
    const out = await getLocationPermission();
    expect(checkPermissions).toHaveBeenCalled();
    expect(out.state).toBe("granted");
    expect(out.background).toBe(false);
    expect(out.source).toBe("capacitor-plugin");
  });

  it("classifies plugin result with foreground only as 'background' downgrade", () => {
    expect(
      classifyStatus({ state: "granted", background: false }),
    ).toBe("background");
  });
});

// ─── subscribeLocationPermission — change event wiring ──────────────

describe("subscribeLocationPermission", () => {
  it("emits an initial snapshot, then re-emits on PermissionStatus change", async () => {
    const status = makeFakePermissionStatus("granted");
    global.navigator = {
      permissions: { query: vi.fn().mockResolvedValue(status) },
    };
    const callback = vi.fn();
    const unsubscribe = subscribeLocationPermission(callback);
    // Initial snapshot is async (resolves through the promise chain).
    await Promise.resolve();
    await Promise.resolve();
    expect(callback).toHaveBeenCalled();
    expect(callback.mock.calls[0][0].state).toBe("granted");

    // Flip and fire onchange.
    status.state = "denied";
    status._fire();
    const lastCallArgs = callback.mock.calls[callback.mock.calls.length - 1];
    expect(lastCallArgs[0].state).toBe("denied");

    unsubscribe();
    // After unsubscribe, no further callbacks even on additional fires.
    callback.mockClear();
    status.state = "granted";
    status._fire();
    expect(callback).not.toHaveBeenCalled();
  });

  it("strips internal _raw field from the user callback payload", async () => {
    // Regression: _raw is an internal field we use for the onchange
    // wiring; it must NOT leak into user code (the consumer should see
    // a clean Status object).
    const status = makeFakePermissionStatus("granted");
    global.navigator = {
      permissions: { query: vi.fn().mockResolvedValue(status) },
    };
    const callback = vi.fn();
    subscribeLocationPermission(callback);
    await Promise.resolve();
    await Promise.resolve();
    const firstArg = callback.mock.calls[0][0];
    expect(firstArg).toBeDefined();
    expect(firstArg._raw).toBeUndefined();
  });
});
