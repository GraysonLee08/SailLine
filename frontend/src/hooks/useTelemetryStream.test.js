// frontend/src/hooks/useTelemetryStream.test.js
//
// Tests for the telemetry WebSocket reconnect manager. Pins the contract
// the Step-4-client wiring will rely on: backoff, watchdog, auth retry,
// resume_from_t, and the send() / state surface.
//
// Strategy
//   - Hand-rolled MockWebSocket replaces globalThis.WebSocket. Captures
//     every constructed socket so we can reach in after a reconnect.
//   - Firebase auth is module-mocked; tests set `auth.currentUser` per
//     case (fresh object each beforeEach to avoid bleed).
//   - Fake timers drive backoff + watchdog deterministically. Microtasks
//     are flushed via vi.advanceTimersByTimeAsync(0).
//
// Out of scope: backoff *delay value* — there's jitter, so we assert
// only that a reconnect occurs within a generous window.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

// Module mock must be declared before importing the hook.
vi.mock("../firebase", () => ({
    auth: { currentUser: null },
}));

import { auth } from "../firebase";
import { useTelemetryStream } from "./useTelemetryStream";

// ─── MockWebSocket ──────────────────────────────────────────────────
//
// Browser-shaped fake. `close()` schedules onclose on the microtask
// queue (matching real browser behavior so the hook's state machine
// runs the same path it would in production). The test-only helpers
// `simulateOpen / simulateMessage / simulateServerClose` skip that
// and fire synchronously so timing-sensitive cases stay readable.

class MockWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    static instances = [];
    static reset() {
        this.instances = [];
    }

    constructor(url) {
        this.url = url;
        this.readyState = MockWebSocket.CONNECTING;
        this.sent = [];
        this.closedWith = null;
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        this.onclose = null;
        MockWebSocket.instances.push(this);
    }

    send(data) {
        if (this.readyState !== MockWebSocket.OPEN) {
            throw new Error("MockWebSocket not open");
        }
        this.sent.push(data);
    }

    close(code, reason) {
        this.closedWith = { code, reason };
        if (this.readyState === MockWebSocket.CLOSED) return;
        this.readyState = MockWebSocket.CLOSED;
        queueMicrotask(() => {
            this.onclose?.({ code: code ?? 1000, reason: reason ?? "" });
        });
    }

    // ── Test-only helpers ──
    simulateOpen() {
        this.readyState = MockWebSocket.OPEN;
        this.onopen?.();
    }

    simulateMessage(payload) {
        const data =
            typeof payload === "string" ? payload : JSON.stringify(payload);
        this.onmessage?.({ data });
    }

    simulateServerClose(code, reason = "") {
        this.readyState = MockWebSocket.CLOSED;
        this.onclose?.({ code, reason });
    }
}

// Flush microtasks + any due timers without advancing simulated time.
const flush = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.reset();
    globalThis.WebSocket = MockWebSocket;
    auth.currentUser = {
        getIdToken: vi.fn().mockResolvedValue("tok"),
    };
});

afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
});

// Render the hook and drive it to the OPEN state. Returns the result
// object, an unmount fn, and the first ws instance.
async function renderOpen({ raceId = "r1" } = {}) {
    let result;
    let unmount;
    await act(async () => {
        const r = renderHook(() =>
            useTelemetryStream({ raceId, enabled: true })
        );
        result = r.result;
        unmount = r.unmount;
        await flush();
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    const ws = MockWebSocket.instances[0];
    await act(async () => {
        ws.simulateOpen();
    });
    expect(result.current.status).toBe("open");
    return { result, unmount, ws };
}

// ─── Gating ─────────────────────────────────────────────────────────

describe("useTelemetryStream — gating", () => {
    it("stays idle and constructs no socket when enabled=false", async () => {
        const { result } = renderHook(() =>
            useTelemetryStream({ raceId: "r1", enabled: false })
        );
        await act(flush);
        expect(result.current.status).toBe("idle");
        expect(MockWebSocket.instances).toHaveLength(0);
    });

    it("stays idle and constructs no socket when raceId is null", async () => {
        const { result } = renderHook(() =>
            useTelemetryStream({ raceId: null, enabled: true })
        );
        await act(flush);
        expect(result.current.status).toBe("idle");
        expect(MockWebSocket.instances).toHaveLength(0);
    });

    it("sets status=error when there is no authenticated user", async () => {
        auth.currentUser = null;
        const { result } = renderHook(() =>
            useTelemetryStream({ raceId: "r1", enabled: true })
        );
        await act(flush);
        expect(result.current.status).toBe("error");
        expect(result.current.error).toMatch(/auth/i);
        expect(MockWebSocket.instances).toHaveLength(0);
    });
});

// ─── Happy path ─────────────────────────────────────────────────────

describe("useTelemetryStream — happy path", () => {
    it("transitions idle → connecting → open", async () => {
        let result;
        await act(async () => {
            const r = renderHook(() =>
                useTelemetryStream({ raceId: "r1", enabled: true })
            );
            result = r.result;
            await flush();
        });
        expect(result.current.status).toBe("connecting");

        const ws = MockWebSocket.instances[0];
        await act(async () => {
            ws.simulateOpen();
        });
        expect(result.current.status).toBe("open");
        expect(result.current.attempt).toBe(0);
    });

    it("opens with token in URL and no resume_from_t on first connect", async () => {
        const { ws } = await renderOpen();
        const url = new URL(ws.url);
        expect(url.searchParams.get("token")).toBe("tok");
        expect(url.searchParams.has("resume_from_t")).toBe(false);
        expect(url.pathname).toBe("/api/races/r1/telemetry/stream");
    });

    it("send() writes JSON and returns true when open", async () => {
        const { result, ws } = await renderOpen();
        const sample = { t: 1, ax: 0, ay: 0, az: 9.8, gx: 0, gy: 0, gz: 0 };
        let returned;
        act(() => {
            returned = result.current.send(sample);
        });
        expect(returned).toBe(true);
        expect(ws.sent).toEqual([JSON.stringify(sample)]);
    });

    it("send() returns false when not open", async () => {
        let result;
        await act(async () => {
            const r = renderHook(() =>
                useTelemetryStream({ raceId: "r1", enabled: true })
            );
            result = r.result;
            await flush();
        });
        // Socket is in CONNECTING (not yet opened) — send must refuse.
        let returned;
        act(() => {
            returned = result.current.send({ t: 0 });
        });
        expect(returned).toBe(false);
    });

    it("updates attitude state from attitude messages", async () => {
        const { result, ws } = await renderOpen();
        await act(async () => {
            ws.simulateMessage({
                type: "attitude",
                t: 12.5,
                heel_deg: 7.2,
                pitch_deg: -1.4,
            });
        });
        expect(result.current.attitude).toEqual({
            t: 12.5,
            heelDeg: 7.2,
            pitchDeg: -1.4,
        });
    });

    it("heartbeat resets the watchdog without changing attitude", async () => {
        const { result, ws } = await renderOpen();
        // Tick close to the 30s watchdog, then deliver a heartbeat.
        await act(async () => {
            await vi.advanceTimersByTimeAsync(20_000);
            ws.simulateMessage({ type: "heartbeat", t: 20 });
        });
        expect(result.current.attitude).toBeNull();
        // 20 more seconds (40 since open, 20 since heartbeat). Watchdog
        // must NOT have fired — same socket, no close.
        await act(async () => {
            await vi.advanceTimersByTimeAsync(20_000);
        });
        expect(MockWebSocket.instances).toHaveLength(1);
        expect(ws.closedWith).toBeNull();
        expect(result.current.status).toBe("open");
    });

    it("closes with code 1000 on unmount", async () => {
        const { unmount, ws } = await renderOpen();
        await act(async () => {
            unmount();
            await flush();
        });
        expect(ws.closedWith?.code).toBe(1000);
    });
});

// ─── Reconnect ──────────────────────────────────────────────────────

describe("useTelemetryStream — reconnect", () => {
    it("schedules a reconnect after a 1006 close", async () => {
        const { result, ws } = await renderOpen();
        await act(async () => {
            ws.simulateServerClose(1006);
            await flush();
        });
        expect(result.current.status).toBe("reconnecting");
        expect(result.current.attempt).toBe(1);
        // First backoff is ~1000 ms ±25%; 1500 ms guarantees fire.
        expect(MockWebSocket.instances).toHaveLength(1);
        await act(async () => {
            await vi.advanceTimersByTimeAsync(1500);
        });
        expect(MockWebSocket.instances).toHaveLength(2);
    });

    it("resets attempt counter after a successful reopen", async () => {
        const { result, ws } = await renderOpen();
        await act(async () => {
            ws.simulateServerClose(1006);
            await flush();
            await vi.advanceTimersByTimeAsync(1500);
        });
        const ws2 = MockWebSocket.instances[1];
        await act(async () => {
            ws2.simulateOpen();
        });
        expect(result.current.status).toBe("open");
        expect(result.current.attempt).toBe(0);
    });

    it("includes resume_from_t in URL on reconnect after an attitude", async () => {
        const { ws } = await renderOpen();
        await act(async () => {
            ws.simulateMessage({
                type: "attitude",
                t: 42.5,
                heel_deg: 0,
                pitch_deg: 0,
            });
            ws.simulateServerClose(1006);
            await flush();
            await vi.advanceTimersByTimeAsync(1500);
        });
        const ws2 = MockWebSocket.instances[1];
        const url = new URL(ws2.url);
        expect(url.searchParams.get("resume_from_t")).toBe("42.5");
    });

    it("watchdog closes a silent socket and triggers reconnect", async () => {
        const { result, ws } = await renderOpen();
        // 30s of silence — no attitude, no heartbeat — watchdog fires.
        await act(async () => {
            await vi.advanceTimersByTimeAsync(30_000);
            await flush();
        });
        expect(ws.closedWith?.code).toBe(4000);
        expect(result.current.status).toBe("reconnecting");
        await act(async () => {
            await vi.advanceTimersByTimeAsync(1500);
        });
        expect(MockWebSocket.instances).toHaveLength(2);
    });
});

// ─── Auth ───────────────────────────────────────────────────────────

describe("useTelemetryStream — auth", () => {
    it("force-refreshes the token after a single 1008", async () => {
        const { ws } = await renderOpen();
        const getToken = auth.currentUser.getIdToken;
        // Initial token fetch used cached (false).
        expect(getToken).toHaveBeenLastCalledWith(false);

        await act(async () => {
            ws.simulateServerClose(1008, "stale token");
            await flush();
            await vi.advanceTimersByTimeAsync(1500);
        });
        // Reconnect refetch passes force=true.
        expect(getToken).toHaveBeenLastCalledWith(true);
        expect(MockWebSocket.instances).toHaveLength(2);
    });

    it("gives up with status=error after two consecutive 1008s", async () => {
        const { result, ws } = await renderOpen();
        await act(async () => {
            ws.simulateServerClose(1008, "first");
            await flush();
            await vi.advanceTimersByTimeAsync(1500);
        });
        // ws2 was constructed but NOT opened — counter still at 1.
        const ws2 = MockWebSocket.instances[1];
        await act(async () => {
            ws2.simulateServerClose(1008, "second");
            await flush();
        });
        expect(result.current.status).toBe("error");
        expect(result.current.error).toMatch(/auth/i);
        // No further reconnect scheduled.
        await act(async () => {
            await vi.advanceTimersByTimeAsync(10_000);
        });
        expect(MockWebSocket.instances).toHaveLength(2);
    });

    it("resets the 1008 counter when a non-auth close intervenes", async () => {
        // Sequence: 1008 → 1006 → 1008. The middle 1006 should zero the
        // counter, so the second 1008 lands as "first" again — retry, not
        // error.
        const { result, ws } = await renderOpen();
        await act(async () => {
            ws.simulateServerClose(1008, "first");
            await flush();
            await vi.advanceTimersByTimeAsync(1500); // attempt 1: ~1s
        });
        const ws2 = MockWebSocket.instances[1];
        await act(async () => {
            ws2.simulateServerClose(1006);
            await flush();
            await vi.advanceTimersByTimeAsync(2500); // attempt 2: ~2s
        });
        const ws3 = MockWebSocket.instances[2];
        await act(async () => {
            ws3.simulateServerClose(1008, "third");
            await flush();
        });
        expect(result.current.status).toBe("reconnecting");
        expect(result.current.error).toBeNull();
    });
});

// ─── Robustness ─────────────────────────────────────────────────────

describe("useTelemetryStream — robustness", () => {
    it("ignores malformed JSON messages", async () => {
        const { result, ws } = await renderOpen();
        await act(async () => {
            ws.simulateMessage("not-json");
            ws.simulateMessage("{");
        });
        expect(result.current.status).toBe("open");
        expect(result.current.attitude).toBeNull();
    });

    it("sets status=error when getIdToken rejects", async () => {
        auth.currentUser = {
            getIdToken: vi.fn().mockRejectedValue(new Error("network")),
        };
        const { result } = renderHook(() =>
            useTelemetryStream({ raceId: "r1", enabled: true })
        );
        await act(flush);
        expect(result.current.status).toBe("error");
        expect(result.current.error).toBe("network");
        expect(MockWebSocket.instances).toHaveLength(0);
    });
});
