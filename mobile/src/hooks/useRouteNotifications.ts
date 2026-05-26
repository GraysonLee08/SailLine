// hooks/useRouteNotifications.ts — SSE stream of "better route" alerts.
//
// Port of frontend/src/hooks/useRouteNotifications.js.
//
// Uses @microsoft/fetch-event-source rather than React Native's
// EventSource (the browser-native one isn't shipped with RN, and
// fetch-event-source supports Authorization headers which we need for
// Firebase auth — the endpoint requires the same ID token as every
// other API call).
//
// The library does its own auto-reconnect on transient errors. Auth
// errors (401/404) are surfaced through onerror via a sentinel error
// type so we stop the stream and surface a real message.

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchEventSource } from "@microsoft/fetch-event-source";

import { auth } from "../firebase";
import type { RouteFeature } from "../api/routing";

// Reuse the existing API base resolution from src/api.ts. Duplicated here
// rather than importing to avoid a circular-ish module load — src/api.ts
// is small and the constant is set at process start, so the duplication
// has no maintenance cost beyond keeping these two lines aligned.
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

class FatalError extends Error {}

export function useRouteNotifications(raceId: string | null) {
  const [alternative, setAlternative] = useState<AlternativePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ctrlRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!raceId) return;

    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    setError(null);

    (async () => {
      const user = auth.currentUser;
      if (!user) {
        setError("Not authenticated");
        return;
      }
      try {
        const token = await user.getIdToken();
        await fetchEventSource(
          `${API_URL}/api/routing/notifications/${raceId}`,
          {
            method: "GET",
            headers: { Authorization: `Bearer ${token}` },
            signal: ctrl.signal,
            openWhenHidden: true,
            async onopen(response) {
              if (response.status === 401 || response.status === 404) {
                throw new FatalError(
                  response.status === 401
                    ? "Not authorized for this race"
                    : "Race not found",
                );
              }
              if (!response.ok) throw new Error(`HTTP ${response.status}`);
              setError(null);
            },
            onmessage(msg) {
              if (msg.event !== "alternative") return;
              try {
                const payload = JSON.parse(msg.data) as AlternativePayload;
                setAlternative(payload);
              } catch (e) {
                // eslint-disable-next-line no-console
                console.error("[useRouteNotifications] bad payload", e);
              }
            },
            onerror(err) {
              if (err instanceof FatalError) {
                setError(err.message);
                throw err; // stop the retry loop
              }
              // Transient — let the lib retry.
            },
          },
        );
      } catch (e) {
        if (e instanceof Error && e.name !== "AbortError") {
          setError(e.message);
        }
      }
    })();

    return () => {
      ctrl.abort();
      ctrlRef.current = null;
    };
  }, [raceId]);

  const dismiss = useCallback(() => setAlternative(null), []);

  const accept = useCallback(
    (onAccept: (route: RouteFeature) => void) => {
      if (!alternative) return;
      onAccept(alternative.route);
      setAlternative(null);
    },
    [alternative],
  );

  return { alternative, accept, dismiss, error };
}
