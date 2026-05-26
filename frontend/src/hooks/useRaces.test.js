// Tests for useRaces.
//
// Mocks ../api so the hook's fetch calls are intercepted. The hook is
// shaped like a small CRUD cache: load on mount, locally mutate on
// create/remove without an extra round-trip. Tests verify each branch.

import { renderHook, act, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { useRaces } from "./useRaces";

const mockApiFetch = vi.fn();
vi.mock("../api", () => ({
  apiFetch: (...args) => mockApiFetch(...args),
}));

beforeEach(() => {
  mockApiFetch.mockReset();
});

function race(id, overrides = {}) {
  return {
    id,
    name: `Race ${id}`,
    mode: "buoy",
    boat_class: "J/105",
    marks: [],
    start_at: null,
    started_at: null,
    ended_at: null,
    uses_spinnaker: false,
    user_id: "uid-1",
    created_at: "2026-05-01T12:00:00Z",
    updated_at: "2026-05-01T12:00:00Z",
    ...overrides,
  };
}

describe("useRaces", () => {
  test("loads on mount and exposes the list", async () => {
    mockApiFetch.mockResolvedValueOnce([race("a"), race("b")]);

    const { result } = renderHook(() => useRaces());

    await waitFor(() => expect(result.current.races).not.toBeNull());
    expect(result.current.races).toHaveLength(2);
    expect(result.current.races[0].id).toBe("a");
    expect(result.current.error).toBeNull();
    expect(mockApiFetch).toHaveBeenCalledWith("/api/races");
  });

  test("captures error message on initial load failure", async () => {
    mockApiFetch.mockRejectedValueOnce(new Error("boom"));

    const { result } = renderHook(() => useRaces());

    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error).toBe("boom");
    expect(result.current.races).toBeNull();
  });

  test("create prepends to local list without a refetch", async () => {
    mockApiFetch.mockResolvedValueOnce([race("a")]); // initial
    const created = race("new");
    mockApiFetch.mockResolvedValueOnce(created);     // POST response

    const { result } = renderHook(() => useRaces());
    await waitFor(() => expect(result.current.races).not.toBeNull());

    await act(async () => {
      await result.current.create({ name: "New" });
    });

    expect(result.current.races).toHaveLength(2);
    // Newly created races land at the top.
    expect(result.current.races[0].id).toBe("new");
    expect(mockApiFetch).toHaveBeenLastCalledWith("/api/races", {
      method: "POST",
      body: { name: "New" },
    });
    // Two calls total — initial GET + the POST. No third GET.
    expect(mockApiFetch).toHaveBeenCalledTimes(2);
  });

  test("remove drops by id without a refetch", async () => {
    mockApiFetch.mockResolvedValueOnce([race("a"), race("b"), race("c")]);
    mockApiFetch.mockResolvedValueOnce(null);

    const { result } = renderHook(() => useRaces());
    await waitFor(() => expect(result.current.races).not.toBeNull());

    await act(async () => {
      await result.current.remove("b");
    });

    expect(result.current.races.map((r) => r.id)).toEqual(["a", "c"]);
    expect(mockApiFetch).toHaveBeenLastCalledWith("/api/races/b", {
      method: "DELETE",
    });
    expect(mockApiFetch).toHaveBeenCalledTimes(2);
  });

  test("create that rejects bubbles up and doesn't touch the list", async () => {
    mockApiFetch.mockResolvedValueOnce([race("a")]);
    mockApiFetch.mockRejectedValueOnce(new Error("server full"));

    const { result } = renderHook(() => useRaces());
    await waitFor(() => expect(result.current.races).not.toBeNull());

    await expect(
      act(async () => {
        await result.current.create({ name: "X" });
      }),
    ).rejects.toThrow("server full");

    // Local list unchanged.
    expect(result.current.races).toHaveLength(1);
    expect(result.current.races[0].id).toBe("a");
  });
});
