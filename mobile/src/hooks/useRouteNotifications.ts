// hooks/useRouteNotifications.ts — SSE stream of "better route" alerts.
//
// Critical race-day feature: the route-recompute worker republishes a
// faster route mid-race when wind shifts make the original plan stale.
// Without this, the user is sailing a plan computed against pre-race
// forecast data that may diverge sharply from what's actually happening.
//
// Why react-native-sse (not @microsoft/fetch-event-source):
//
// The web frontend uses @microsoft/fetch-event-source because the
// browser-native EventSource API can't attach an Authorization header
// (required for our Firebase ID-token auth). On React Native that
// library is unusable: it calls `document.addEventListener` for tab-
// visibility handling, and RN has no `document`.
//
// react-native-sse fills the same need (EventSource API + custom
// headers) but is RN-native. Same auth contract, same event shape —
// only the import + listener attach syntax differ.

import { useCallback, useEffect, useRef, useState } from "react";
import EventSource from "react-native-sse";

import { auth } from "../firebase";
import type { RouteFeature } from "../api/routing";

const PROD_API_URL =
  "https://sailline-api-105706282249.us-central1.run.app";
const API_URL = process.env.EXPO_PUBLIC_API_URL || PROD_API_URL;

export type AlternativePayload = {
  race_id: string;
  old_total_minutes: number;
  new_total_minutes: number;
  improvement_minutes: number;
  improvement_pct: number;
  route: RouteFeature;
  computed_at: string;
};

/** AI tactician call (2026-06-11) — published by the backend tactics
 *  pipeline on the same per-race channel, surfaced as its own named
 *  SSE event. Shape mirrors backend pipeline._evaluate's payload. */
export type TacticsPayload = {
  type: "tactics";
  race_id: string;
  call_type: string;
  call_class: "maneuver" | "coaching";
  message: string;
  eta: string | null;
  diagnosis: Record<string, unknown>;
  model: string;
  prompt_version: number;
  created_at: string;
};

// Named events the server emits on this stream. "alternative" carries
// better-route payloads (legacy shape, no type field); "tactics"
// carries AI tactician calls.
type StreamEventType = "alternative" | "tactics";

export function useRouteNotifications(raceId: string | null) {
  const [alternative, setAlternative] = useState<AlternativePayload | null>(null);
  const [tactics, setTactics] = useState<TacticsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const esRef = useRef<EventSource<StreamEventType> | null>(null);

  useEffect(() => {
    if (!raceId) return;

    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    setError(null);

    const connect = async (attempt: number): Promise<void> => {
      if (cancelled) return;

      const user = auth.currentUser;
      if (!user) {
        setError("Not authenticated");
        return;
      }

      // Always fetch a fresh token on (re)connect. Firebase ID tokens
      // expire after 1 hour; a long race means the token captured at
      // the initial connect will be stale by the time react-native-sse
      // auto-reconnects (e.g., after a Redis timeout drops the stream).
      // Reusing the stale token → 401 → "Not authorized" → lost route
      // guidance for the rest of the race. This was Observation 4 from
      // the Silly Race.
      let token: string;
      try {
        token = await user.getIdToken(true); // forceRefresh = true
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
        return;
      }
      if (cancelled) return;

      const es = new EventSource<StreamEventType>(
        `${API_URL}/api/routing/notifications/${raceId}`,
        {
          headers: { Authorization: `Bearer ${token}` },
          debug: false,
          pollingInterval: 0,
          lineEndingCharacter: "\n",
        },
      );
      esRef.current = es;

      es.addEventListener("open", () => {
        if (!cancelled) setError(null);
      });

      es.addEventListener("alternative", (event) => {
        if (cancelled) return;
        const raw = typeof event.data === "string" ? event.data : "";
        if (!raw) return;
        try {
          const payload = JSON.parse(raw) as AlternativePayload;
          setAlternative(payload);
        } catch (e) {
          // eslint-disable-next-line no-console
          console.error("[useRouteNotifications] bad payload", e);
        }
      });

      es.addEventListener("tactics", (event) => {
        if (cancelled) return;
        const raw = typeof event.data === "string" ? event.data : "";
        if (!raw) return;
        try {
          const payload = JSON.parse(raw) as TacticsPayload;
          setTactics(payload);
        } catch (e) {
          // eslint-disable-next-line no-console
          console.error("[useRouteNotifications] bad tactics payload", e);
        }
      });

      es.addEventListener("error", (event) => {
        if (cancelled) return;
        const status = (event as { xhrStatus?: number }).xhrStatus;
        if (status === 401) {
          // Token expired or invalid — close and reconnect with a
          // fresh token instead of dying permanently. Exponential
          // backoff capped at 30s to avoid hammering the server.
          es.close();
          const delay = Math.min(2000 * Math.pow(2, attempt), 30000);
          reconnectTimer = setTimeout(() => void connect(attempt + 1), delay);
        } else if (status === 404) {
          setError("Race not found");
          es.close();
        }
        // Other errors: let the library retry silently.
      });
    };

    void connect(0);

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      const es = esRef.current;
      esRef.current = null;
      if (es) {
        try {
          es.removeAllEventListeners();
          es.close();
        } catch {
          /* best effort */
        }
      }
    };
  }, [raceId]);

  const dismiss = useCallback(() => setAlternative(null), []);

  const dismissTactics = useCallback(() => setTactics(null), []);

  const accept = useCallback(
    (onAccept: (route: RouteFeature) => void) => {
      if (!alternative) return;
      onAccept(alternative.route);
      setAlternative(null);
    },
    [alternative],
  );

  return { alternative, accept, dismiss, tactics, dismissTactics, error };
}
