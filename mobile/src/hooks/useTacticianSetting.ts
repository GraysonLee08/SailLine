// useTacticianSetting.ts — per-race AI Tactician display toggle.
//
// Default is ON for the v1a rollout: the backend only evaluates for
// Pro-tier users, so free users never receive calls regardless of this
// flag. The toggle gates the CLIENT surfaces (TacticianCard + local
// notification); a global server-side opt-out also exists via
// app_settings.tactician.enabled (see backend pipeline), which the
// Settings screen can expose later.
//
// Mirrors useAutoRouteSetting exactly — same AsyncStorage prefix
// scheme, same hydration contract.

import AsyncStorage from "@react-native-async-storage/async-storage";
import { useCallback, useEffect, useState } from "react";

const KEY_PREFIX = "sailline:tactician:";
const DEFAULT_ENABLED = true;

function keyFor(raceId: string): string {
  return `${KEY_PREFIX}${raceId}`;
}

export function useTacticianSetting(raceId: string | null) {
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
