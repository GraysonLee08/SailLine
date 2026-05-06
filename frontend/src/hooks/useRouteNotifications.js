// frontend/src/hooks/useRouteNotifications.js
//
// SSE subscription to /api/routing/notifications/{raceId} for the
// "better route available" stream. Uses @microsoft/fetch-event-source
// rather than the browser's native EventSource because EventSource
// can't attach an Authorization header — and our endpoint requires
// the Firebase ID token like every other API call.
//
// Returns:
//   alternative   the most recent unaccepted alternative payload, or null
//   accept(fn)    invokes fn(routeFeature), then clears the alternative
//   dismiss()     clears the alternative without applying it
//   error         connection error string, or null
//
// Lifecycle: opens on raceId mount, closes on unmount or raceId change.
// The library auto-reconnects on transient network errors. Auth errors
// terminate the stream and surface via `error`.

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { auth } from "../firebase";

const API_URL = import.meta.env.VITE_API_URL || "";

class FatalError extends Error {}

export function useRouteNotifications(raceId) {
  const [alternative, setAlternative] = useState(null);
  const [error, setError] = useState(null);
  const ctrlRef = useRef(null);

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

            // Keep the connection alive when the tab is backgrounded.
            // Sailors will switch tabs / lock screens during a race;
            // we want them to come back to a live banner if a better
            // route landed in the meantime.
            openWhenHidden: true,

            async onopen(response) {
              if (response.status === 401 || response.status === 404) {
                throw new FatalError(
                  response.status === 401
                    ? "Not authorized for this race"
                    : "Race not found",
                );
              }
              if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
              }
              setError(null);
            },

            onmessage(msg) {
              if (msg.event !== "alternative") return;
              try {
                const payload = JSON.parse(msg.data);
                setAlternative(payload);
              } catch (e) {
                console.error("[useRouteNotifications] bad payload", e);
              }
            },

            onerror(err) {
              // FatalError stops the auto-retry loop. Anything else
              // returns from this handler, signalling the library to
              // back off and reconnect.
              if (err instanceof FatalError) {
                setError(err.message);
                throw err;
              }
              // Transient — let the lib retry. Don't spam the console.
            },
          },
        );
      } catch (e) {
        if (e.name !== "AbortError") {
          setError(e.message || "Connection failed");
        }
      }
    })();

    return () => {
      ctrl.abort();
      ctrlRef.current = null;
    };
  }, [raceId]);

  const dismiss = useCallback(() => {
    setAlternative(null);
  }, []);

  const accept = useCallback(
    (onAccept) => {
      if (!alternative) return;
      if (typeof onAccept === "function") {
        onAccept(alternative.route);
      }
      setAlternative(null);
    },
    [alternative],
  );

  return { alternative, accept, dismiss, error };
}
