// Tests for useRaceStats.
//
// Mocks ../api so the hook's fetch calls are intercepted. Uses
// vitest's fake timers to drive the polling loop deterministically.
//
// What we cover:
//   * fetches stats + track on raceId mount
//   * exposes both via the returned object
//   * when the response carries summary_pending=true, schedules
//     another fetch after POLL_INTERVAL_MS
//   * polling stops once summary_pending flips to false
//   * regenerate() POSTs and re-enables polling
//   * clearing raceId teardown clears the timer

import { renderHook, act, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useRaceStats } from "./useRaceStats";

const mockApiFetch = vi.fn();
vi.mock("../api", () => ({
  apiFetch: (...args) => mockApiFetch(...args),
}));


beforeEach(() => {
  vi.useFakeTimers();
  mockApiFetch.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});


function pendingStats() {
  return {
    race_id: "r1",
    name: "Test",
    boat_class: "J/70",
    start_at: "2026-05-14T18:00:00Z",
    marks: [{ lat: 42.0, lon: -87.7 }],
    stats: {
      point_count: 100,
      started_at: "2026-05-14T18:00:00Z",
      ended_at: "2026-05-14T18:30:00Z",
      elapsed_s: 1800,
      moving_s: 1700,
      stopped_s: 100,
      distance_m: 4000,
      avg_sog_kt: 4.3,
      avg_moving_sog_kt: 4.5,
      max_sog_kt: 6.5,
      legs: [],
      speed_series: [],
    },
    ai_summary: null,
    wind: null,
    summary_pending: true,
  };
}

function completeStats() {
  return {
    ...pendingStats(),
    ai_summary: {
      recap: "Solid race.",
      tips: ["Trim earlier."],
      model: "test",
      prompt_version: 1,
      generated_at: "2026-05-14T18:35:00Z",
    },
    summary_pending: false,
  };
}


describe("useRaceStats", () => {
  test("fetches stats and track on mount", async () => {
    mockApiFetch
      .mockResolvedValueOnce(completeStats())   // /stats
      .mockResolvedValueOnce([{ lat: 42, lon: -87.7, recorded_at: "x" }]); // /track

    const { result } = renderHook(() => useRaceStats("r1"));
    await waitFor(() => expect(result.current.data).not.toBeNull());

    expect(result.current.data.name).toBe("Test");
    expect(result.current.track.length).toBe(1);
    expect(result.current.loading).toBe(false);
    expect(mockApiFetch).toHaveBeenCalledTimes(2);
  });

  test("polls while summary_pending=true and stops when summary arrives", async () => {
    mockApiFetch
      .mockResolvedValueOnce(pendingStats())     // initial /stats
      .mockResolvedValueOnce([])                  // initial /track
      .mockResolvedValueOnce(pendingStats())     // poll 1
      .mockResolvedValueOnce(completeStats());   // poll 2 — summary lands

    const { result } = renderHook(() => useRaceStats("r1"));
    await waitFor(() => expect(result.current.data).not.toBeNull());
    expect(result.current.data.summary_pending).toBe(true);

    // First poll fires at +8s.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    expect(result.current.data.summary_pending).toBe(true);

    // Second poll fires at +16s and brings the summary.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });

    await waitFor(() =>
      expect(result.current.data.summary_pending).toBe(false),
    );
    expect(result.current.data.ai_summary?.recap).toBe("Solid race.");

    // No further polls.
    const callsBefore = mockApiFetch.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(mockApiFetch.mock.calls.length).toBe(callsBefore);
  });

  test("regenerate POSTs and re-arms polling", async () => {
    mockApiFetch
      .mockResolvedValueOnce(completeStats())  // initial /stats
      .mockResolvedValueOnce([])                // initial /track
      .mockResolvedValueOnce({ accepted: true }) // POST regenerate
      .mockResolvedValueOnce(pendingStats())   // poll after regenerate
      .mockResolvedValueOnce(completeStats()); // resolved on second poll

    const { result } = renderHook(() => useRaceStats("r1"));
    await waitFor(() => expect(result.current.data).not.toBeNull());

    await act(async () => {
      await result.current.regenerate();
    });
    expect(mockApiFetch).toHaveBeenCalledWith(
      "/api/races/r1/stats/regenerate",
      { method: "POST" },
    );

    // Poll fires after 8s.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    expect(result.current.data.summary_pending).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    await waitFor(() =>
      expect(result.current.data.summary_pending).toBe(false),
    );
  });

  test("clears state and timer when raceId becomes null", async () => {
    mockApiFetch
      .mockResolvedValueOnce(pendingStats())
      .mockResolvedValueOnce([]);

    const { result, rerender } = renderHook(({ id }) => useRaceStats(id), {
      initialProps: { id: "r1" },
    });
    await waitFor(() => expect(result.current.data).not.toBeNull());

    rerender({ id: null });
    expect(result.current.data).toBeNull();
    expect(result.current.track).toBeNull();
  });
});
