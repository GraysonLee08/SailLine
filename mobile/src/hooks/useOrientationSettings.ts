// useOrientationSettings.ts — per-race phone axis + calibration storage.
//
// Mirrors the web app's `calibrationStorageKey` pattern (one entry per
// race id). Persisted to AsyncStorage so the user only has to "Zero"
// once per race, even across app restarts. Cleared when the race ends
// (caller's job — we just expose set/clear).
//
// Keys are scoped by raceId so two boats sharing one phone (unlikely
// but possible at a club regatta) don't share calibration.

import AsyncStorage from "@react-native-async-storage/async-storage";
import { useCallback, useEffect, useState } from "react";

import type { Calibration, PhoneAxis } from "./useHeelGauge";

type Stored = {
  phoneAxis: PhoneAxis;
  calibration: Calibration | null;
};

const KEY_PREFIX = "sailline:orientation:";

function keyFor(raceId: string): string {
  return `${KEY_PREFIX}${raceId}`;
}

const DEFAULT: Stored = {
  phoneAxis: "fore-aft",
  calibration: null,
};

export function useOrientationSettings(raceId: string | null) {
  const [phoneAxis, setPhoneAxisState] = useState<PhoneAxis>(DEFAULT.phoneAxis);
  const [calibration, setCalibrationState] = useState<Calibration | null>(
    DEFAULT.calibration,
  );
  const [hydrated, setHydrated] = useState(false);

  // Hydrate on race switch.
  useEffect(() => {
    let cancelled = false;
    setHydrated(false);
    if (!raceId) {
      setPhoneAxisState(DEFAULT.phoneAxis);
      setCalibrationState(DEFAULT.calibration);
      setHydrated(true);
      return () => {
        cancelled = true;
      };
    }
    (async () => {
      try {
        const raw = await AsyncStorage.getItem(keyFor(raceId));
        if (cancelled) return;
        if (raw) {
          const parsed = JSON.parse(raw) as Partial<Stored>;
          setPhoneAxisState(parsed.phoneAxis === "port-stbd" ? "port-stbd" : "fore-aft");
          setCalibrationState(parsed.calibration ?? null);
        } else {
          setPhoneAxisState(DEFAULT.phoneAxis);
          setCalibrationState(DEFAULT.calibration);
        }
      } catch {
        // Storage errors are non-fatal — fall back to defaults.
        if (!cancelled) {
          setPhoneAxisState(DEFAULT.phoneAxis);
          setCalibrationState(DEFAULT.calibration);
        }
      } finally {
        if (!cancelled) setHydrated(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [raceId]);

  const persist = useCallback(
    async (next: Stored) => {
      if (!raceId) return;
      try {
        await AsyncStorage.setItem(keyFor(raceId), JSON.stringify(next));
      } catch {
        /* non-fatal */
      }
    },
    [raceId],
  );

  const setPhoneAxis = useCallback(
    (axis: PhoneAxis) => {
      setPhoneAxisState(axis);
      void persist({ phoneAxis: axis, calibration });
    },
    [calibration, persist],
  );

  const setCalibration = useCallback(
    (cal: Calibration | null) => {
      setCalibrationState(cal);
      void persist({ phoneAxis, calibration: cal });
    },
    [phoneAxis, persist],
  );

  return {
    phoneAxis,
    calibration,
    hydrated,
    setPhoneAxis,
    setCalibration,
  };
}
