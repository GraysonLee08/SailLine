// frontend/src/hooks/useTelemetryStream.js
//
// Client-side reconnect manager for the telemetry WebSocket.
//
// Backend: WS /api/races/{raceId}/telemetry/stream?token=<id_token>
//                                                 [&resume_from_t=<float>]
//          - Client → server: IMU samples (JSON text frames), via `send()`
//          - Server → client: {type:"attitude",t,heel_deg,pitch_deg}
//                             {type:"heartbeat",t}  every 15s
//
// Why this exists (dev plan §3.2 "Starlink Simulation"):
//   Cloud Run caps each WS at 60 min, and 5G/Starlink will drop packets
//   on a long race. This hook owns the lifecycle so the rest of the app
//   (sensor sampler + heel gauge) doesn't have to think about it. The
//   historical track is owned by the REST /telemetry batch path, so a
//   reconnect just resumes the live stream — nothing is lost.
//
// Returns:
//   status     "idle" | "connecting" | "open" | "reconnecting" | "error"
//   attitude   { t, heelDeg, pitchDeg } | null
//   attempt    reconnect attempt counter (resets on successful open)
//   error      string | null  (set when we give up retrying)
//   send(s)    push an IMU sample; drops silently when not open
//
// Usage:
//   const { status, attitude, send } = useTelemetryStream({ raceId, enabled: recording });
//   // ...wire to the sampler:
//   start({ rateHz: 10, onSample: (s) => send(s) });
//
// Close-code policy:
//   1000 normal       → caller initiated (unmount / enabled=false); stop.
//   1008 policy/auth  → refresh Firebase token & retry once; second
//                       consecutive 1008 → status="error", stop.
//   1001/1006/1011/*  → reconnect with exponential backoff + jitter.
//   4000 "watchdog"   → our own force-close after 30s of server silence;
//                       routed through the normal reconnect path.

import { useCallback, useEffect, useRef, useState } from "react";
import { auth } from "../firebase";

const API_URL = import.meta.env.VITE_API_URL || "";

// Backoff schedule (ms). Caps at 30s — beyond that, retries become more
// annoying than useful, and the watchdog will catch any half-open state.
const BACKOFF_SCHEDULE_MS = [1000, 2000, 4000, 8000, 16000, 30000];
const JITTER_FRACTION = 0.25;

// Server sends heartbeats every 15s. We give 2× headroom so a single
// missed heartbeat doesn't force a reconnect, but a real half-open TCP
// connection (common on 5G handoffs) is caught quickly.
const HEARTBEAT_WATCHDOG_MS = 30_000;

// Private close code (4000–4999 range) we use when the watchdog tears
// down a silent connection. Distinguishable from server-initiated codes
// in logs, but otherwise routed through the normal reconnect path.
const WATCHDOG_CLOSE_CODE = 4000;

function wsBaseFromApiUrl() {
    // Empty API_URL → same-origin (Firebase Hosting rewrites /api/** in prod).
    // Otherwise flip the scheme http(s) → ws(s).
    if (!API_URL) {
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        return `${proto}//${window.location.host}`;
    }
    return API_URL.replace(/^http/, "ws");
}

function backoffDelayMs(attemptIndex) {
    const base =
        BACKOFF_SCHEDULE_MS[Math.min(attemptIndex, BACKOFF_SCHEDULE_MS.length - 1)];
    const jitter = base * JITTER_FRACTION;
    // ±25% jitter so reconnects from many clients (or many tabs) don't
    // synchronize on a server-side restart.
    return base + (Math.random() * 2 - 1) * jitter;
}

/**
 * @param {object} opts
 * @param {string|null} opts.raceId  Race UUID. null/undefined → idle.
 * @param {boolean} opts.enabled     Gate. false → cleanly close & idle.
 */
export function useTelemetryStream({ raceId, enabled }) {
    const [status, setStatus] = useState("idle");
    const [attitude, setAttitude] = useState(null);
    const [attempt, setAttempt] = useState(0);
    const [error, setError] = useState(null);

    // Mutable internals — kept in refs so they don't trigger re-renders
    // and so the send() callback can stay reference-stable.
    const wsRef = useRef(null);
    const lastTRef = useRef(null);          // last attitude.t for resume_from_t
    const watchdogRef = useRef(null);
    const reconnectTimerRef = useRef(null);
    const attemptRef = useRef(0);
    const authFailuresRef = useRef(0);      // consecutive 1008s

    const send = useCallback((sample) => {
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return false;
        try {
            ws.send(JSON.stringify(sample));
            return true;
        } catch {
            // Buffer full or socket closed mid-send. Caller doesn't retry —
            // REST batch is the source of truth for storage; WS is live-only.
            return false;
        }
    }, []);

    useEffect(() => {
        if (!enabled || !raceId) {
            setStatus("idle");
            setAttitude(null);
            setError(null);
            setAttempt(0);
            attemptRef.current = 0;
            authFailuresRef.current = 0;
            lastTRef.current = null;
            return undefined;
        }

        // Effect-scoped cancellation. Survives async gaps inside connect().
        let cancelled = false;

        const clearWatchdog = () => {
            if (watchdogRef.current) {
                clearTimeout(watchdogRef.current);
                watchdogRef.current = null;
            }
        };

        const armWatchdog = () => {
            clearWatchdog();
            watchdogRef.current = setTimeout(() => {
                const ws = wsRef.current;
                if (ws && ws.readyState !== WebSocket.CLOSED) {
                    try {
                        ws.close(WATCHDOG_CLOSE_CODE, "watchdog");
                    } catch {
                        /* nothing to do; onclose will still fire */
                    }
                }
            }, HEARTBEAT_WATCHDOG_MS);
        };

        const scheduleReconnect = () => {
            if (cancelled) return;
            const nextAttempt = attemptRef.current + 1;
            attemptRef.current = nextAttempt;
            setAttempt(nextAttempt);
            setStatus("reconnecting");
            const delay = backoffDelayMs(nextAttempt - 1);
            reconnectTimerRef.current = setTimeout(() => {
                reconnectTimerRef.current = null;
                connect();
            }, delay);
        };

        const connect = async () => {
            if (cancelled) return;

            setStatus(attemptRef.current === 0 ? "connecting" : "reconnecting");
            setError(null);

            const user = auth.currentUser;
            if (!user) {
                setStatus("error");
                setError("Not authenticated");
                return;
            }

            // Force refresh after an auth failure; otherwise let Firebase
            // serve cached. ID tokens are short-lived (~1h), and a stale
            // token at reconnect time is the most common 1008 cause.
            let token;
            try {
                token = await user.getIdToken(authFailuresRef.current > 0);
            } catch (e) {
                setStatus("error");
                setError(e.message || "Token fetch failed");
                return;
            }
            if (cancelled) return;

            const params = new URLSearchParams({ token });
            if (lastTRef.current != null) {
                // Server currently logs this but doesn't act on it; sending it
                // anyway establishes the protocol for future gap-detection /
                // replay without a URL change.
                params.set("resume_from_t", String(lastTRef.current));
            }
            const url =
                `${wsBaseFromApiUrl()}/api/races/${raceId}/telemetry/stream?${params}`;

            let ws;
            try {
                ws = new WebSocket(url);
            } catch {
                scheduleReconnect();
                return;
            }
            wsRef.current = ws;

            ws.onopen = () => {
                if (cancelled) {
                    try {
                        ws.close(1000, "cancelled");
                    } catch {
                        /* ignore */
                    }
                    return;
                }
                setStatus("open");
                setAttempt(0);
                attemptRef.current = 0;
                authFailuresRef.current = 0;
                armWatchdog();
            };

            ws.onmessage = (ev) => {
                // Any frame from the server proves the connection is alive.
                armWatchdog();
                let msg;
                try {
                    msg = JSON.parse(ev.data);
                } catch {
                    return;
                }
                if (msg.type === "attitude") {
                    if (typeof msg.t === "number") lastTRef.current = msg.t;
                    setAttitude({
                        t: msg.t,
                        heelDeg: msg.heel_deg,
                        pitchDeg: msg.pitch_deg,
                    });
                }
                // heartbeat: watchdog reset above is the whole response.
            };

            ws.onerror = () => {
                // Browsers fire close() after error(); let onclose handle the
                // state machine so we don't double-count attempts.
            };

            ws.onclose = (ev) => {
                clearWatchdog();
                if (wsRef.current === ws) wsRef.current = null;

                if (cancelled) {
                    setStatus("idle");
                    return;
                }

                // 1000 with our own reasons → caller stopped (unmount or
                // enabled=false). Don't reconnect. Watchdog uses 4000 so it
                // doesn't accidentally hit this branch.
                if (ev.code === 1000) {
                    setStatus("idle");
                    return;
                }

                // 1008 = policy / auth (expired/invalid token, race-not-owned).
                // First time: force-refresh token & retry. Second time: give up.
                if (ev.code === 1008) {
                    authFailuresRef.current += 1;
                    if (authFailuresRef.current >= 2) {
                        setStatus("error");
                        setError(`Auth failed: ${ev.reason || "policy violation"}`);
                        return;
                    }
                } else {
                    // Any non-auth close resets the auth-failure counter — we
                    // only care about *consecutive* 1008s.
                    authFailuresRef.current = 0;
                }

                scheduleReconnect();
            };
        };

        connect();

        return () => {
            cancelled = true;
            clearWatchdog();
            if (reconnectTimerRef.current) {
                clearTimeout(reconnectTimerRef.current);
                reconnectTimerRef.current = null;
            }
            const ws = wsRef.current;
            if (ws && ws.readyState !== WebSocket.CLOSED) {
                try {
                    ws.close(1000, "unmount");
                } catch {
                    /* ignore */
                }
            }
            wsRef.current = null;
        };
    }, [enabled, raceId]);

    return { status, attitude, attempt, error, send };
}