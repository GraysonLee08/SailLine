// frontend/src/hooks/useAutoStartRecorder.test.js
//
// Pins the arming behaviour for the T-5 auto-start recorder hook.
//
// What we assert (in priority order):
//   1. Fires exactly once at start_at - 5min when enabled.
//   2. Idempotent against the recorder — if recording is already true
//      at fire time, start() is NOT called a second time.
//   3. Honours `enabled=false` — never fires.
//   4. Re-arms cleanly when start_at slips later mid-session.
//   5. Fires immediately when mounted inside the T-5 window.
//   6. Does NOT retro-fire when mounted >10min past gun time.
//
// Strategy: vitest fake timers drive setTimeout so the test runs in ms.
// renderHook re-mounts cheaply, and props re-renders use rerender so
// the same hook instance observes the prop change (matches reality
// where MapView re-renders when start_at changes).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

import { useAutoStartRecorder } from "./useAutoStartRecorder";

const RACE_ID = "race-1";

function isoOffsetFromNow(ms) {
  return new Date(Date.now() + ms).toISOString();
}

describe("useAutoStartRecorder", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Pin clock to a deterministic instant so isoOffsetFromNow is stable
    // across the test's setTimeout advances.
    vi.setSystemTime(new Date("2026-05-14T18:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("fires start() once at start_at - 5min", () => {
    const start = vi.fn();
    // start_at is 6 minutes from "now" → arming should happen in 1 minute.
    const startAtIso = isoOffsetFromNow(6 * 60 * 1000);
    renderHook(() =>
      useAutoStartRecorder({
        raceId: RACE_ID,
        startAtIso,
        enabled: true,
        recording: false,
        start,
      }),
    );
    expect(start).not.toHaveBeenCalled();

    // Advance up to (but not past) the arming instant — still no fire.
    act(() => {
      vi.advanceTimersByTime(60 * 1000 - 1);
    });
    expect(start).not.toHaveBeenCalled();

    // Cross the arming instant.
    act(() => {
      vi.advanceTimersByTime(2);
    });
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("does not double-fire if recording is already true at fire time", () => {
    const start = vi.fn();
    const startAtIso = isoOffsetFromNow(6 * 60 * 1000);
    const { rerender } = renderHook(
      ({ recording }) =>
        useAutoStartRecorder({
          raceId: RACE_ID,
          startAtIso,
          enabled: true,
          recording,
          start,
        }),
      { initialProps: { recording: false } },
    );

    // User hits Record manually before the timer fires.
    rerender({ recording: true });
    act(() => {
      vi.advanceTimersByTime(2 * 60 * 1000);
    });
    expect(start).not.toHaveBeenCalled();
  });

  it("never fires when enabled=false", () => {
    const start = vi.fn();
    const startAtIso = isoOffsetFromNow(6 * 60 * 1000);
    renderHook(() =>
      useAutoStartRecorder({
        raceId: RACE_ID,
        startAtIso,
        enabled: false,
        recording: false,
        start,
      }),
    );
    act(() => {
      vi.advanceTimersByTime(10 * 60 * 1000);
    });
    expect(start).not.toHaveBeenCalled();
  });

  it("re-arms when start_at is pushed back mid-session", () => {
    const start = vi.fn();
    const first = isoOffsetFromNow(6 * 60 * 1000);
    // 15 min after the original start_at value.
    const later = isoOffsetFromNow(6 * 60 * 1000 + 15 * 60 * 1000);
    const { rerender } = renderHook(
      ({ startAtIso }) =>
        useAutoStartRecorder({
          raceId: RACE_ID,
          startAtIso,
          enabled: true,
          recording: false,
          start,
        }),
      { initialProps: { startAtIso: first } },
    );

    // Push start_at later 30 seconds in.
    act(() => {
      vi.advanceTimersByTime(30 * 1000);
    });
    rerender({ startAtIso: later });

    // Original arming instant was at +60s; we should NOT fire there now.
    act(() => {
      vi.advanceTimersByTime(60 * 1000);
    });
    expect(start).not.toHaveBeenCalled();

    // New arming instant: later (= +21m) - 5min = +16m from t0.
    // We're at +90s; need another 16m - 90s = 14m 30s.
    act(() => {
      vi.advanceTimersByTime(14 * 60 * 1000 + 30 * 1000 + 1);
    });
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("fires immediately when mounted inside the T-5 window", () => {
    const start = vi.fn();
    // start_at is 2 minutes from now → we're inside the arming window.
    const startAtIso = isoOffsetFromNow(2 * 60 * 1000);
    renderHook(() =>
      useAutoStartRecorder({
        raceId: RACE_ID,
        startAtIso,
        enabled: true,
        recording: false,
        start,
      }),
    );
    // Hook schedules a 0ms timeout for "fire on next tick".
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("does not retro-fire when mounted >10min past start_at", () => {
    const start = vi.fn();
    // Start was 30 minutes ago — race is in progress or done.
    const startAtIso = isoOffsetFromNow(-30 * 60 * 1000);
    renderHook(() =>
      useAutoStartRecorder({
        raceId: RACE_ID,
        startAtIso,
        enabled: true,
        recording: false,
        start,
      }),
    );
    act(() => {
      vi.advanceTimersByTime(60 * 1000);
    });
    expect(start).not.toHaveBeenCalled();
  });
});
