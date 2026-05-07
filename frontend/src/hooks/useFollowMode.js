// frontend/src/hooks/useFollowMode.js
//
// Geolocation follow-mode state for the active-race map view. Models
// Google-Maps-style "follow me" — the camera tracks the user's
// position until the user manually pans/zooms/rotates, at which point
// follow flips off. A "Re-center" pill button (rendered by MapView)
// lets the user re-engage.
//
// Persisted per-raceId in sessionStorage so a reload mid-race
// preserves intent. The non-null raceId argument IS the "race is
// active" gate — the hook is only ever instantiated with a real id.

import { useCallback, useEffect, useState } from "react";

const KEY = (raceId) => `sailline.follow:${raceId}`;

export function useFollowMode(raceId) {
  const [following, setFollowingState] = useState(() => {
    if (!raceId) return false;
    if (typeof sessionStorage === "undefined") return true;
    const stored = sessionStorage.getItem(KEY(raceId));
    if (stored === "1") return true;
    if (stored === "0") return false;
    return true;  // default for new race
  });

  const setFollowing = useCallback(
    (value) => {
      setFollowingState(value);
      if (raceId && typeof sessionStorage !== "undefined") {
        sessionStorage.setItem(KEY(raceId), value ? "1" : "0");
      }
    },
    [raceId],
  );

  // Reset state when raceId changes — different race, different
  // persisted preference.
  useEffect(() => {
    if (!raceId) {
      setFollowingState(false);
      return;
    }
    if (typeof sessionStorage === "undefined") return;
    const stored = sessionStorage.getItem(KEY(raceId));
    if (stored === "1") setFollowingState(true);
    else if (stored === "0") setFollowingState(false);
    else setFollowingState(true);
  }, [raceId]);

  // recenter() is a higher-level convenience: flips following on AND
  // signals to MapView that a one-shot re-pan should happen. We
  // surface a counter that increments on each call so MapView's
  // effect can react to the bump without coupling to internal state.
  const [recenterTick, setRecenterTick] = useState(0);
  const recenter = useCallback(() => {
    setFollowing(true);
    setRecenterTick((n) => n + 1);
  }, [setFollowing]);

  return { following, setFollowing, recenter, recenterTick };
}
