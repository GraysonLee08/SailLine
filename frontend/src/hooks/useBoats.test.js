// Tests for useBoats.
//
// Mocks ../api so apiFetch is intercepted. uploadCert uses raw fetch
// (not apiFetch) because it needs multipart, so we mock fetch + auth
// for the cert test.

import { renderHook, act, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useBoats } from "./useBoats";

const mockApiFetch = vi.fn();
vi.mock("../api", () => ({
  apiFetch: (...args) => mockApiFetch(...args),
}));

// auth is used by uploadCert only; stub it with a token-returning user.
vi.mock("../firebase", () => ({
  auth: {
    currentUser: {
      getIdToken: vi.fn().mockResolvedValue("test-token"),
    },
  },
}));

beforeEach(() => {
  mockApiFetch.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function boat(id, overrides = {}) {
  return {
    id,
    name: `Boat ${id}`,
    boat_class: "J/105",
    sail_number: "USA 1",
    ...overrides,
  };
}

describe("useBoats", () => {
  test("loads boats on mount", async () => {
    mockApiFetch.mockResolvedValueOnce([boat("a"), boat("b")]);

    const { result } = renderHook(() => useBoats());

    await waitFor(() => expect(result.current.boats).not.toBeNull());
    expect(result.current.boats).toHaveLength(2);
    expect(result.current.error).toBeNull();
    expect(mockApiFetch).toHaveBeenCalledWith("/api/boats");
  });

  test("captures error on initial load failure", async () => {
    mockApiFetch.mockRejectedValueOnce(new Error("no perms"));

    const { result } = renderHook(() => useBoats());

    await waitFor(() => expect(result.current.error).toBe("no perms"));
    expect(result.current.boats).toBeNull();
  });

  test("create prepends and avoids a refetch", async () => {
    mockApiFetch.mockResolvedValueOnce([boat("a")]);
    const created = boat("new", { name: "New Boat" });
    mockApiFetch.mockResolvedValueOnce(created);

    const { result } = renderHook(() => useBoats());
    await waitFor(() => expect(result.current.boats).not.toBeNull());

    await act(async () => {
      await result.current.create({ name: "New Boat" });
    });

    expect(result.current.boats).toHaveLength(2);
    expect(result.current.boats[0].id).toBe("new");
    expect(mockApiFetch).toHaveBeenLastCalledWith("/api/boats", {
      method: "POST",
      body: { name: "New Boat" },
    });
    expect(mockApiFetch).toHaveBeenCalledTimes(2);
  });

  test("update replaces by id locally", async () => {
    mockApiFetch.mockResolvedValueOnce([boat("a"), boat("b")]);
    const updated = boat("b", { name: "Renamed" });
    mockApiFetch.mockResolvedValueOnce(updated);

    const { result } = renderHook(() => useBoats());
    await waitFor(() => expect(result.current.boats).not.toBeNull());

    await act(async () => {
      await result.current.update("b", { name: "Renamed" });
    });

    expect(result.current.boats.find((b) => b.id === "b").name).toBe("Renamed");
    // 'a' is untouched.
    expect(result.current.boats.find((b) => b.id === "a").name).toBe("Boat a");
    expect(mockApiFetch).toHaveBeenLastCalledWith("/api/boats/b", {
      method: "PATCH",
      body: { name: "Renamed" },
    });
  });

  test("remove drops by id", async () => {
    mockApiFetch.mockResolvedValueOnce([boat("a"), boat("b")]);
    mockApiFetch.mockResolvedValueOnce(null);

    const { result } = renderHook(() => useBoats());
    await waitFor(() => expect(result.current.boats).not.toBeNull());

    await act(async () => {
      await result.current.remove("a");
    });

    expect(result.current.boats.map((b) => b.id)).toEqual(["b"]);
    expect(mockApiFetch).toHaveBeenLastCalledWith("/api/boats/a", {
      method: "DELETE",
    });
  });

  test("uploadCert posts multipart and returns parsed body", async () => {
    mockApiFetch.mockResolvedValueOnce([boat("a")]);

    // Stub global fetch for the cert upload path.
    const fakeFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ phrf: 99, sail_number: "USA 7" }),
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useBoats());
    await waitFor(() => expect(result.current.boats).not.toBeNull());

    const file = new Blob(["fake-pdf"], { type: "application/pdf" });
    let parsed;
    await act(async () => {
      parsed = await result.current.uploadCert("a", file);
    });

    expect(parsed).toEqual({ phrf: 99, sail_number: "USA 7" });

    expect(fakeFetch).toHaveBeenCalledTimes(1);
    const [url, init] = fakeFetch.mock.calls[0];
    expect(url).toMatch(/\/api\/boats\/a\/cert$/);
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer test-token");
    // body is a FormData; verify it carries a 'file' field. FormData
    // wraps an unnamed Blob into a File internally, so we can't assert
    // identity against the original Blob — check the content shape
    // instead. (File extends Blob, so instanceof Blob also matches.)
    expect(init.body).toBeInstanceOf(FormData);
    const stored = init.body.get("file");
    expect(stored).toBeInstanceOf(Blob);
    expect(stored.size).toBe(file.size);
    expect(stored.type).toBe(file.type);
  });

  test("uploadCert throws on non-ok response", async () => {
    mockApiFetch.mockResolvedValueOnce([boat("a")]);

    const fakeFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 415,
      text: vi.fn().mockResolvedValue("unsupported"),
    });
    vi.stubGlobal("fetch", fakeFetch);

    const { result } = renderHook(() => useBoats());
    await waitFor(() => expect(result.current.boats).not.toBeNull());

    const file = new Blob(["x"], { type: "application/pdf" });
    await expect(
      act(async () => {
        await result.current.uploadCert("a", file);
      }),
    ).rejects.toThrow(/415/);
  });
});
