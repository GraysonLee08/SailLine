// useAutoPassSetting.ts — global "auto-detect mark passes" preference.
//
// Storage shape mirrors useAutoRouteSetting (AsyncStorage, hydrated
// flag) BUT this one is a GLOBAL pref, not per-race. The user wanted a
// single toggle in app settings that applies to every race — not a
// per-race choice they'd need to set each time.
//
// Semantics — "auto-pass" on the CLIENT side only:
//   * ON  (default): the recording screen runs `useMissedMarkNotifier`,
//     surfacing watch notifications when the boat plausibly passed a
//     mark the v3 detector missed.
//   * OFF: notifier hook is skipped. Server-side auto-detection still
//     runs (it's a per-race flag on the race row, not a user pref) —
//     marks still appear on the pill row as the detector finds them.
//     This toggle is purely about the CLIENT alerting the user.
//
// 2026-06-03 — added per B2 of the mobile-fixes plan. Wired into the
// SettingsScreen and into `app/(app)/recording.tsx`. The notifier hook
// stays mounted unconditionally (always call the same hooks); we just
// disable its alert path when the setting is OFF.

import AsyncStorage from "@react-native-async-storage/async-storage";
import { useCallback, useEffect, useState } from "react";

const KEY = "sailline:autoPass:global";
const DEFAULT_ENABLED = true;

export function useAutoPassSetting() {
  const [enabled, setEnabledState] = useState<boolean>(DEFAULT_ENABLED);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const raw = await AsyncStorage.getItem(KEY);
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
  }, []);

  const setEnabled = useCallback((next: boolean) => {
    setEnabledState(next);
    (async () => {
      try {
        await AsyncStorage.setItem(KEY, next ? "true" : "false");
      } catch {
        /* non-fatal */
      }
    })();
  }, []);

  return { enabled, setEnabled, hydrated };
}
