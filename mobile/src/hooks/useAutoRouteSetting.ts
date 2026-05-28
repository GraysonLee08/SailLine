// useAutoRouteSetting.ts — per-race auto-accept toggle for better routes.
//
// Default is ON: when a better-route alert arrives, the banner counts
// down 10s and auto-accepts unless the user taps Decline. Stored per
// raceId so an opt-out on one race doesn't bleed into the next.
//
// Mirrors the storage pattern of `useOrientationSettings` — same prefix
// scheme, AsyncStorage, hydration flag — but for a single boolean.

import AsyncStorage from "@react-native-async-storage/async-storage";
import { useCallback, useEffect, useState } from "react";

const KEY_PREFIX = "sailline:autoRoute:";
const DEFAULT_ENABLED = true;

function keyFor(raceId: string): string {
  return `${KEY_PREFIX}${raceId}`;
}

export function useAutoRouteSetting(raceId: string | null) {
  const [enabled, setEnabledState] = useState<boolean>(DEFAULT_ENABLED);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setHydrated(false);
    if (!raceId) {
      setEnabledState(DEFAULT_ENABLED);
      setHydrated(true);
      return () => {
        cancelled = true;
      };
    }
    (async () => {
      try {
        const raw = await AsyncStorage.getItem(keyFor(raceId));
        if (cancelled) return;
        if (raw === "true") setEnabledState(true);
        else if (raw === "false") setEnabledState(false);
        else setEnabledState(DEFAULT_ENABLED);
      } catch {
        if (!cancelled) setEnabledState(DEFAULT_ENABLED);
      } finally {
        if (!cancelled) setHydrated(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [raceId]);

  const setEnabled = useCallback(
    (next: boolean) => {
      setEnabledState(next);
      if (!raceId) return;
      (async () => {
        try {
          await AsyncStorage.setItem(keyFor(raceId), next ? "true" : "false");
        } catch {
          /* non-fatal */
        }
      })();
    },
    [raceId],
  );

  return { enabled, setEnabled, hydrated };
}
