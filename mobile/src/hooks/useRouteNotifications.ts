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
    setError(null);

    (async () => {
      const user = auth.currentUser;
      if (!user) {
        setError("Not authenticated");
        return;
      }

      let token: string;
      try {
        token = await user.getIdToken();
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
        return;
      }
      if (cancelled) return;

      // react-native-sse's EventSource exposes the standard "open" /
      // "message" / "error" / "close" lifecycle PLUS any named events
      // we register via the generic type parameter ("alternative").
      const es = new EventSource<StreamEventType>(
        `${API_URL}/api/routing/notifications/${raceId}`,
        {
          headers: { Authorization: `Bearer ${token}` },
          // Library defaults: reconnect on transient errors with backoff.
          // Don't pollute log with the routine reconnects.
          debug: false,
          pollingInterval: 0, // 0 = true streaming, not long-polling
          // FastAPI + sse-starlette emit LF (\n) line endings. Without
          // this hint react-native-sse can't auto-detect the delimiter
          // on the initial frame and logs "Unable to identify the line
          // ending character" instead of parsing events.
          lineEndingCharacter: "\n",
        },
      );
      esRef.current = es;

      es.addEventListener("open", () => {
        if (!cancelled) setError(null);
      });

      es.addEventListener("alternative", (event) => {
        if (cancelled) return;
        // event.data is the raw SSE data field — a JSON-encoded
        // AlternativePayload, same shape as the web client receives.
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

      // AI tactician calls (2026-06-11). Same stream, own named event;
      // latest-call-wins (mid-race only the newest call matters — the
      // server's cooldowns guarantee they're minutes apart).
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
        // react-native-sse fires "error" for both transient network
        // hiccups (the lib auto-retries) and terminal HTTP errors. The
        // shape carries `type` + sometimes `status`. Surface only the
        // terminal cases so the UI doesn't flicker during reconnects.
        const status = (event as { xhrStatus?: number }).xhrStatus;
        if (status === 401) {
          setError("Not authorized for this race");
          es.close();
        } else if (status === 404) {
          setError("Race not found");
          es.close();
        }
        // Other errors: let the library retry silently.
      });
    })();

    return () => {
      cancelled = true;
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
