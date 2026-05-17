// useTrackRecorder.test.js — recorder migration + IMU + calibration.
//
// The recorder mixes a lot of async surfaces: geolocation watcher,
// orientation listener, fetch flush, Wake Lock, localStorage. We mock
// the leaves and assert behaviour at the hook level via React Testing
// Library's act + renderHook.
//
// Coverage focus:
//   * POST goes to /telemetry with the new payload shape
//   * IMU samples are queued and flushed alongside GPS
//   * Pending calibration is included in the next flush, then cleared
//   * Wake Lock is requested on start, released on stop
//   * iOS permission flow — "denied" still allows GPS recording
//   * Per-race localStorage scoping

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTrackRecorder } from "./useTrackRecorder";

// ─── Mocks ──────────────────────────────────────────────────────────

// apiFetch — we capture calls and resolve happily by default.
const apiFetchMock = vi.fn(async () => ({ ok: true }));
vi.mock("../api", () => ({
  apiFetch: (...args) => apiFetchMock(...args),
}));

// createWatcher — we capture the onPosition callback and expose a way
// for tests to inject fake fixes.
let lastWatcherArgs = null;
let watcherStopped = false;
vi.mock("../lib/geolocation", () => ({
  createWatcher: vi.fn(async (args) => {
    lastWatcherArgs = args;
    watcherStopped = false;
    return {
      stop: async () => {
        watcherStopped = true;
      },
    };
  }),
}));

// orientation — controllable cached reading.
let latestOrientation = null;
let orientationStarts = 0;
let orientationStops = 0;
vi.mock("../sensors/orientation", () => ({
  isSupported: () => true,
  needsPermissionPrompt: () => false,
  requestPermission: async () => "not-needed",
  start: () => {
    orientationStarts += 1;
    return {
      stop: () => {
        orientationStops += 1;
      },
    };
  },
  latest: () => latestOrientation,
}));

// Wake Lock API — minimal sentinel that just records release calls.
const wakeLockReleases = { count: 0 };
function installFakeWakeLock() {
  globalThis.navigator.wakeLock = {
    request: vi.fn(async () => ({
      addEventListener: () => {},
      release: async () => {
        wakeLockReleases.count += 1;
      },
    })),
  };
}

// Fake timers shared across tests so flush intervals can be advanced.
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  apiFetchMock.mockClear();
  lastWatcherArgs = null;
  watcherStopped = false;
  latestOrientation = null;
  orientationStarts = 0;
  orientationStops = 0;
  wakeLockReleases.count = 0;
  installFakeWakeLock();
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  delete globalThis.navigator.wakeLock;
});

// ─── Tests ──────────────────────────────────────────────────────────

const RACE_ID = "race-uuid-1";

function emitGpsFix(point) {
  // The recorder's adapter normalises to this shape, so we hand it the
  // normalised shape directly.
  lastWatcherArgs.onPosition({
    recorded_at: "2026-05-16T18:00:00.000Z",
    lat: 42.0,
    lon: -87.5,
    speed_kts: 5.0,
    heading_deg: 90.0,
    gps_acc_m: 4.0,
    ...point,
  });
}

describe("useTrackRecorder", () => {
  it("flushes to /telemetry with the new payload shape", async () => {
    const { result } = renderHook(() => useTrackRecorder(RACE_ID));

    await act(async () => {
      await result.current.start();
    });

    // Need at least one GPS fix to motivate a flush.
    act(() => {
      emitGpsFix({
        recorded_at: "2026-05-16T18:00:01.000Z",
        speed_kts: 4.2,
        heading_deg: 95,
      });
    });

    await act(async () => {
      await result.current.flushNow();
    });

    expect(apiFetchMock).toHaveBeenCalled();
    const [url, opts] = apiFetchMock.mock.calls[apiFetchMock.mock.calls.length - 1];
    expect(url).toBe(`/api/races/${RACE_ID}/telemetry`);
    expect(opts.method).toBe("POST");
    expect(opts.body).toMatchObject({ gps: expect.any(Array), imu: expect.any(Array) });
    expect(opts.body.gps[0]).toMatchObject({
      t: "2026-05-16T18:00:01.000Z",
      lat: 42.0,
      lon: -87.5,
      sog_kts: 4.2,
      cog_deg: 95,
      gps_acc_m: 4.0,
    });
  });

  it("queues IMU samples from the orientation listener and flushes them", async () => {
    const { result } = renderHook(() => useTrackRecorder(RACE_ID));

    await act(async () => {
      await result.current.start();
    });

    // Feed an orientation reading and let the 10Hz sampler tick.
    latestOrientation = { alpha: 45, beta: 3, gamma: 14 };
    await act(async () => {
      vi.advanceTimersByTime(250); // ~2-3 ticks at 10Hz
    });

    await act(async () => {
      await result.current.flushNow();
    });

    const lastCall = apiFetchMock.mock.calls.at(-1);
    expect(lastCall[1].body.imu.length).toBeGreaterThan(0);
    const sample = lastCall[1].body.imu[0];
    expect(sample).toHaveProperty("t");
    expect(sample.heel_deg).toBeCloseTo(14, 5);
    expect(sample.pitch_deg).toBeCloseTo(3, 5);
    expect(sample.yaw_deg).toBeCloseTo(45, 5);
  });

  it("ships a captureCalibration() result in the next flush and clears it", async () => {
    const { result } = renderHook(() => useTrackRecorder(RACE_ID));
    await act(async () => {
      await result.current.start();
    });
    latestOrientation = { alpha: 0, beta: 2.5, gamma: -1.5 };
    // Allow sampler to register the reading.
    await act(async () => {
      vi.advanceTimersByTime(100);
    });

    let captured;
    act(() => {
      captured = result.current.captureCalibration();
    });
    expect(captured).not.toBeNull();
    expect(captured.heel_zero_offset_deg).toBeCloseTo(-1.5);
    expect(captured.pitch_zero_offset_deg).toBeCloseTo(2.5);

    await act(async () => {
      await result.current.flushNow();
    });

    const body = apiFetchMock.mock.calls.at(-1)[1].body;
    expect(body.calibration).toBeTruthy();
    expect(body.calibration.heel_zero_offset_deg).toBeCloseTo(-1.5);

    // Subsequent flush should NOT re-send.
    apiFetchMock.mockClear();
    emitGpsFix({ recorded_at: "2026-05-16T18:00:02.000Z" });
    await act(async () => {
      await result.current.flushNow();
    });
    const next = apiFetchMock.mock.calls.at(-1)[1].body;
    expect(next.calibration).toBeUndefined();
  });

  it("requests and releases the Wake Lock around start/stop", async () => {
    const { result } = renderHook(() => useTrackRecorder(RACE_ID));
    await act(async () => {
      await result.current.start();
    });
    expect(navigator.wakeLock.request).toHaveBeenCalledWith("screen");

    await act(async () => {
      await result.current.stop();
    });
    expect(wakeLockReleases.count).toBeGreaterThan(0);
  });

  it("records GPS-only when orientation permission is denied", async () => {
    // Re-mock the orientation module to deny permission and require a prompt.
    const orientationMod = await import("../sensors/orientation");
    vi.spyOn(orientationMod, "needsPermissionPrompt").mockReturnValue(true);
    vi.spyOn(orientationMod, "requestPermission").mockResolvedValue("denied");

    const { result } = renderHook(() => useTrackRecorder(RACE_ID));
    await act(async () => {
      await result.current.start();
    });

    latestOrientation = { alpha: 45, beta: 3, gamma: 14 };
    await act(async () => {
      vi.advanceTimersByTime(500);
    });

    emitGpsFix({ recorded_at: "2026-05-16T18:00:01.000Z" });
    await act(async () => {
      await result.current.flushNow();
    });

    const body = apiFetchMock.mock.calls.at(-1)[1].body;
    expect(body.gps.length).toBeGreaterThan(0);
    expect(body.imu.length).toBe(0);
    expect(result.current.orientationPermission).toBe("denied");
  });

  it("scopes localStorage per race so two races don't cross-contaminate", async () => {
    // Prime race A.
    const { result, rerender, unmount } = renderHook(
      ({ id }) => useTrackRecorder(id),
      { initialProps: { id: "race-A" } },
    );
    await act(async () => {
      await result.current.start();
    });
    emitGpsFix({ recorded_at: "2026-05-16T18:00:01.000Z" });
    // Don't flush — leave a pending point in localStorage.

    expect(localStorage.getItem("sailline.trackQueue.race-A")).toBeTruthy();
    expect(localStorage.getItem("sailline.trackQueue.race-B")).toBeNull();

    // Switch to race B; the previous race's pending queue should NOT
    // appear on the new race's recorder.
    await act(async () => {
      await result.current.stop();
    });
    rerender({ id: "race-B" });
    // After re-mount, race-B's queue is empty.
    expect(result.current.queueLength).toBe(0);

    unmount();
  });
});
