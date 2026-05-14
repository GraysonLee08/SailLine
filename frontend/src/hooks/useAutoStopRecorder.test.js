// frontend/src/hooks/useAutoStopRecorder.test.js
//
// Pins the auto-stop behaviour. Strategy mirrors useAutoStartRecorder:
// vitest fake timers, deterministic system time, renderHook for prop
// rerenders.
//
// Track points are constructed with the same geometry helpers as the
// markRounding tests so we can synthesise "rounded mark X at time T"
// scenarios deterministically.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

import { useAutoStopRecorder } from "./useAutoStopRecorder";

const REF_LAT = 42.05;
const REF_LON = -87.75;
const RACE_ID = "race-stop-1";

const mToDLat = (m) => m / 111_000;
const mToDLon = (m, atLat = REF_LAT) =>
  m / (111_000 * Math.cos((atLat * Math.PI) / 180));

function offset(lat, lon, bearingDeg, distM) {
  const rad = (bearingDeg * Math.PI) / 180;
  return {
    lat: lat + mToDLat(distM * Math.cos(rad)),
    lon: lon + mToDLon(distM * Math.sin(rad), lat),
  };
}

function lineThrough(
  mark,
  closestM,
  { spanM = 200, n = 21, bearingDeg = 90, t0 = 0, dtS = 1 } = {},
) {
  const perp = (bearingDeg + 90) % 360;
  const cap = offset(mark.lat, mark.lon, perp, closestM);
  const half = spanM / 2;
  const step = n > 1 ? spanM / (n - 1) : 0;
  const out = [];
  for (let i = 0; i < n; i += 1) {
    const p = offset(cap.lat, cap.lon, bearingDeg, -half + i * step);
    out.push({
      lat: p.lat,
      lon: p.lon,
      ts: new Date((t0 + i * dtS) * 1000).toISOString(),
    });
  }
  return out;
}

describe("useAutoStopRecorder", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Pin to an instant well after the synthesised track timestamps so
    // computed delays are predictable. Track ts uses epoch + offset; we
    // anchor "now" so that the latest pass is exactly LAST_TS_MS in the
    // past at test start.
    vi.setSystemTime(new Date("2026-05-14T18:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not stop on a single-mark course", () => {
    const stop = vi.fn();
    const mark = { lat: REF_LAT, lon: REF_LON };
    // Even with a clear pass, a 1-mark course has no second-to-last
    // → gate is permanently closed.
    const points = lineThrough(mark, 5);

    renderHook(() =>
      useAutoStopRecorder({
        raceId: RACE_ID,
        marks: [mark],
        points,
        recording: true,
        stop,
      }),
    );

    act(() => {
      vi.advanceTimersByTime(10 * 60 * 1000);
    });

    expect(stop).not.toHaveBeenCalled();
  });

  it("does not stop until both last and second-to-last are rounded", () => {
    const stop = vi.fn();
    const a = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 500);
    const b = { lat: bOff.lat, lon: bOff.lon };
    // Round only A; B never gets visited.
    const points = lineThrough(a, 5);

    renderHook(() =>
      useAutoStopRecorder({
        raceId: RACE_ID,
        marks: [a, b],
        points,
        recording: true,
        stop,
      }),
    );

    act(() => {
      vi.advanceTimersByTime(30 * 60 * 1000);
    });

    expect(stop).not.toHaveBeenCalled();
  });

  it("schedules stop 5 minutes after the final rounding", () => {
    const stop = vi.fn();
    const a = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 500);
    const b = { lat: bOff.lat, lon: bOff.lon };

    // Anchor "now" exactly at the moment the final pass would close.
    // Build the track so the final point's ts equals current Date.now().
    const now = Date.now();
    // Last point of the second leg sits ~10s after t0 across 21 steps
    // (t0 + 20s). Set t0 so that (t0 + 20)*1000 === now.
    const t0_b = now / 1000 - 20;
    const t0_a = t0_b - 60;
    const trackA = lineThrough(a, 5, { t0: t0_a });
    const trackB = lineThrough(b, 5, { t0: t0_b });

    renderHook(() =>
      useAutoStopRecorder({
        raceId: RACE_ID,
        marks: [a, b],
        points: [...trackA, ...trackB],
        recording: true,
        stop,
      }),
    );

    // Just before 5 min — no fire.
    act(() => {
      vi.advanceTimersByTime(5 * 60 * 1000 - 100);
    });
    expect(stop).not.toHaveBeenCalled();

    // Cross the 5-min boundary.
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it("fires immediately when mounted long after the final rounding", () => {
    const stop = vi.fn();
    const a = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 500);
    const b = { lat: bOff.lat, lon: bOff.lon };

    // Construct track with last point 30 minutes in the past.
    const lastTsSec = Date.now() / 1000 - 30 * 60;
    const t0_b = lastTsSec - 20;
    const t0_a = t0_b - 60;
    const trackA = lineThrough(a, 5, { t0: t0_a });
    const trackB = lineThrough(b, 5, { t0: t0_b });

    renderHook(() =>
      useAutoStopRecorder({
        raceId: RACE_ID,
        marks: [a, b],
        points: [...trackA, ...trackB],
        recording: true,
        stop,
      }),
    );

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it("does not fire when not recording", () => {
    const stop = vi.fn();
    const a = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 500);
    const b = { lat: bOff.lat, lon: bOff.lon };

    const lastTsSec = Date.now() / 1000 - 30 * 60;
    const t0_b = lastTsSec - 20;
    const trackA = lineThrough(a, 5, { t0: t0_b - 60 });
    const trackB = lineThrough(b, 5, { t0: t0_b });

    renderHook(() =>
      useAutoStopRecorder({
        raceId: RACE_ID,
        marks: [a, b],
        points: [...trackA, ...trackB],
        recording: false,
        stop,
      }),
    );

    act(() => {
      vi.advanceTimersByTime(60 * 1000);
    });
    expect(stop).not.toHaveBeenCalled();
  });

  it("does not fire when enabled=false", () => {
    const stop = vi.fn();
    const a = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 500);
    const b = { lat: bOff.lat, lon: bOff.lon };

    const lastTsSec = Date.now() / 1000 - 30 * 60;
    const t0_b = lastTsSec - 20;
    const trackA = lineThrough(a, 5, { t0: t0_b - 60 });
    const trackB = lineThrough(b, 5, { t0: t0_b });

    renderHook(() =>
      useAutoStopRecorder({
        raceId: RACE_ID,
        marks: [a, b],
        points: [...trackA, ...trackB],
        recording: true,
        enabled: false,
        stop,
      }),
    );

    act(() => {
      vi.advanceTimersByTime(60 * 1000);
    });
    expect(stop).not.toHaveBeenCalled();
  });

  it("stays idempotent on re-render after firing", () => {
    const stop = vi.fn();
    const a = { lat: REF_LAT, lon: REF_LON };
    const bOff = offset(REF_LAT, REF_LON, 0, 500);
    const b = { lat: bOff.lat, lon: bOff.lon };

    const lastTsSec = Date.now() / 1000 - 30 * 60;
    const t0_b = lastTsSec - 20;
    const trackA = lineThrough(a, 5, { t0: t0_b - 60 });
    const trackB = lineThrough(b, 5, { t0: t0_b });
    const allPoints = [...trackA, ...trackB];

    const { rerender } = renderHook(
      ({ points }) =>
        useAutoStopRecorder({
          raceId: RACE_ID,
          marks: [a, b],
          points,
          recording: true,
          stop,
        }),
      { initialProps: { points: allPoints } },
    );

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(stop).toHaveBeenCalledTimes(1);

    // Simulate the recorder appending a few more points after stop()
    // was called — we should not re-fire.
    const morePoints = [
      ...allPoints,
      {
        lat: a.lat,
        lon: a.lon,
        ts: new Date(Date.now()).toISOString(),
      },
    ];
    rerender({ points: morePoints });
    act(() => {
      vi.advanceTimersByTime(10 * 60 * 1000);
    });
    expect(stop).toHaveBeenCalledTimes(1);
  });
});
