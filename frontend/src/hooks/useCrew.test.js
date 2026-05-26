// Tests for useCrew + redeemInvite.
//
// useCrew is shaped slightly differently from useRaces/useBoats: most
// mutations REFETCH rather than locally update, because the server can
// shuffle invite expiry / accepted-by fields in ways the client doesn't
// model. So most tests verify (a) the right URL+body went out and
// (b) refresh was triggered after the mutation.

import { renderHook, act, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { useCrew, redeemInvite } from "./useCrew";

const mockApiFetch = vi.fn();
vi.mock("../api", () => ({
  apiFetch: (...args) => mockApiFetch(...args),
}));

beforeEach(() => {
  mockApiFetch.mockReset();
});

function member(uid, overrides = {}) {
  return { uid, email: `${uid}@example.com`, role: "crew", ...overrides };
}

function invite(code, overrides = {}) {
  return { code, role: "crew", email: null, expires_at: null, ...overrides };
}

describe("useCrew", () => {
  test("does not fetch when boatId is null", () => {
    renderHook(() => useCrew(null));
    expect(mockApiFetch).not.toHaveBeenCalled();
  });

  test("loads members and invites in parallel", async () => {
    mockApiFetch
      .mockResolvedValueOnce([member("u1"), member("u2", { role: "owner" })])
      .mockResolvedValueOnce([invite("AAA")]);

    const { result } = renderHook(() => useCrew("boat-1"));

    await waitFor(() => expect(result.current.members).not.toBeNull());
    expect(result.current.members).toHaveLength(2);
    expect(result.current.invites).toEqual([invite("AAA")]);
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);

    // Both endpoints called once.
    expect(mockApiFetch).toHaveBeenCalledWith("/api/boats/boat-1/crew");
    expect(mockApiFetch).toHaveBeenCalledWith("/api/boats/boat-1/invites");
  });

  test("treats invite-list failure as an empty list (viewer case)", async () => {
    // Members succeed; invites 404 because the caller isn't owner.
    mockApiFetch
      .mockResolvedValueOnce([member("u1")])
      .mockRejectedValueOnce(new Error("forbidden"));

    const { result } = renderHook(() => useCrew("boat-1"));

    await waitFor(() => expect(result.current.members).not.toBeNull());
    expect(result.current.invites).toEqual([]);
    expect(result.current.error).toBeNull(); // viewer is the happy path
  });

  test("captures error when the members fetch fails", async () => {
    mockApiFetch
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce([]);

    const { result } = renderHook(() => useCrew("boat-1"));

    await waitFor(() => expect(result.current.error).toBe("boom"));
    expect(result.current.loading).toBe(false);
  });

  test("updateRole PATCHes and refreshes", async () => {
    mockApiFetch
      .mockResolvedValueOnce([member("u1")])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(null) // PATCH
      .mockResolvedValueOnce([member("u1", { role: "captain" })])
      .mockResolvedValueOnce([]);

    const { result } = renderHook(() => useCrew("boat-1"));
    await waitFor(() => expect(result.current.members).not.toBeNull());

    await act(async () => {
      await result.current.updateRole("u1", "captain");
    });

    expect(mockApiFetch).toHaveBeenCalledWith(
      "/api/boats/boat-1/crew/u1",
      { method: "PATCH", body: { role: "captain" } },
    );
    expect(result.current.members[0].role).toBe("captain");
  });

  test("removeMember DELETEs and refreshes", async () => {
    mockApiFetch
      .mockResolvedValueOnce([member("u1"), member("u2")])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(null) // DELETE
      .mockResolvedValueOnce([member("u2")])
      .mockResolvedValueOnce([]);

    const { result } = renderHook(() => useCrew("boat-1"));
    await waitFor(() => expect(result.current.members).not.toBeNull());

    await act(async () => {
      await result.current.removeMember("u1");
    });

    expect(mockApiFetch).toHaveBeenCalledWith(
      "/api/boats/boat-1/crew/u1",
      { method: "DELETE" },
    );
    expect(result.current.members.map((m) => m.uid)).toEqual(["u2"]);
  });

  test("createInvite POSTs with the right body and returns the result", async () => {
    mockApiFetch
      .mockResolvedValueOnce([member("u1")])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(invite("NEWCODE")) // POST
      .mockResolvedValueOnce([member("u1")])
      .mockResolvedValueOnce([invite("NEWCODE")]);

    const { result } = renderHook(() => useCrew("boat-1"));
    await waitFor(() => expect(result.current.members).not.toBeNull());

    let created;
    await act(async () => {
      created = await result.current.createInvite({
        role: "crew",
        email: "x@y.com",
        expiresInDays: 7,
      });
    });

    expect(created).toEqual(invite("NEWCODE"));
    expect(mockApiFetch).toHaveBeenCalledWith(
      "/api/boats/boat-1/invites",
      {
        method: "POST",
        body: { role: "crew", email: "x@y.com", expires_in_days: 7 },
      },
    );
  });

  test("createInvite omits email/expiry when not passed", async () => {
    mockApiFetch
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(invite("X"))
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([invite("X")]);

    const { result } = renderHook(() => useCrew("boat-1"));
    await waitFor(() => expect(result.current.members).not.toBeNull());

    await act(async () => {
      await result.current.createInvite({ role: "crew" });
    });

    expect(mockApiFetch).toHaveBeenCalledWith(
      "/api/boats/boat-1/invites",
      { method: "POST", body: { role: "crew" } },
    );
  });

  test("revokeInvite DELETEs the code and refreshes", async () => {
    mockApiFetch
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([invite("XYZ")])
      .mockResolvedValueOnce(null) // DELETE
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([]);

    const { result } = renderHook(() => useCrew("boat-1"));
    await waitFor(() => expect(result.current.invites).not.toBeNull());

    await act(async () => {
      await result.current.revokeInvite("XYZ");
    });

    expect(mockApiFetch).toHaveBeenCalledWith(
      "/api/boats/boat-1/invites/XYZ",
      { method: "DELETE" },
    );
    expect(result.current.invites).toEqual([]);
  });

  test("clears state when boatId becomes null", async () => {
    mockApiFetch
      .mockResolvedValueOnce([member("u1")])
      .mockResolvedValueOnce([invite("A")]);

    const { result, rerender } = renderHook(({ id }) => useCrew(id), {
      initialProps: { id: "boat-1" },
    });
    await waitFor(() => expect(result.current.members).not.toBeNull());

    rerender({ id: null });

    expect(result.current.members).toBeNull();
    expect(result.current.invites).toBeNull();
  });
});

describe("redeemInvite", () => {
  test("POSTs the code to /api/invites/redeem", async () => {
    mockApiFetch.mockResolvedValueOnce({ boat_id: "b1", role: "crew" });

    const out = await redeemInvite("AAA111");

    expect(out).toEqual({ boat_id: "b1", role: "crew" });
    expect(mockApiFetch).toHaveBeenCalledWith("/api/invites/redeem", {
      method: "POST",
      body: { code: "AAA111" },
    });
  });
});
