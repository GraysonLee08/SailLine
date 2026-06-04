// useLayerSettings.ts — global preference for what map overlays show.
//
// Drives the LayersPanel (single Layers FAB → expanding popover). Each
// layer has an independent on/off switch persisted to AsyncStorage so
// the choice survives app restarts. Defaults: every layer ON — the
// user explicitly asked for "default being all layers" (2026-06-04).
//
// Storage shape mirrors useAutoPassSetting: one key per layer, "true"
// / "false" string values. Independent keys (not one JSON blob) so a
// future layer can be added without breaking existing users' stored
// state — the migration is "key absent → use the new default."
//
// What's a "layer":
//   * route      — calculated/pre-race route (always rendered in
//                  RouteLayer; toggling this passes null to MapCanvas).
//   * actualRoute — live recorded track polyline on /recording.
//   * wind       — wind barb overlay.
//   * waves      — placeholder. Backend WW3 ingest is not yet shipped;
//                  the LayersPanel renders this row disabled with a
//                  "Soon" badge. We still expose the setting so the
//                  user's pre-toggle survives when the backend lands.
//
// Pattern is intentionally identical to useAutoPassSetting so the
// next developer recognises it without re-reading. Same hydrated
// flag for the "haven't read storage yet" race window.

import AsyncStorage from "@react-native-async-storage/async-storage";
import { useCallback, useEffect, useState } from "react";

const KEY_ROUTE = "sailline:layer:route";
const KEY_ACTUAL_ROUTE = "sailline:layer:actualRoute";
const KEY_WIND = "sailline:layer:wind";
const KEY_WAVES = "sailline:layer:waves";

type LayerVisibility = {
  route: boolean;
  actualRoute: boolean;
  wind: boolean;
  waves: boolean;
};

const DEFAULTS: LayerVisibility = {
  route: true,
  actualRoute: true,
  wind: true,
  waves: true,
};

export type LayerKey = keyof LayerVisibility;

export type UseLayerSettingsResult = LayerVisibility & {
  /** Toggle one layer; persists to AsyncStorage. */
  setLayer: (key: LayerKey, value: boolean) => void;
  /** True once the persisted values have been read at least once. */
  hydrated: boolean;
};

function keyFor(layer: LayerKey): string {
  switch (layer) {
    case "route":
      return KEY_ROUTE;
    case "actualRoute":
      return KEY_ACTUAL_ROUTE;
    case "wind":
      return KEY_WIND;
    case "waves":
      return KEY_WAVES;
  }
}

export function useLayerSettings(): UseLayerSettingsResult {
  const [state, setState] = useState<LayerVisibility>(DEFAULTS);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [route, actualRoute, wind, waves] = await Promise.all([
          AsyncStorage.getItem(KEY_ROUTE),
          AsyncStorage.getItem(KEY_ACTUAL_ROUTE),
          AsyncStorage.getItem(KEY_WIND),
          AsyncStorage.getItem(KEY_WAVES),
        ]);
        if (cancelled) return;
        // "absent" → keep DEFAULT for forward-compat with future layers.
        setState({
          route: route === null ? DEFAULTS.route : route === "true",
          actualRoute:
            actualRoute === null
              ? DEFAULTS.actualRoute
              : actualRoute === "true",
          wind: wind === null ? DEFAULTS.wind : wind === "true",
          waves: waves === null ? DEFAULTS.waves : waves === "true",
        });
      } catch {
        if (!cancelled) setState(DEFAULTS);
      } finally {
        if (!cancelled) setHydrated(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const setLayer = useCallback((key: LayerKey, value: boolean) => {
    setState((prev) => ({ ...prev, [key]: value }));
    (async () => {
      try {
        await AsyncStorage.setItem(keyFor(key), value ? "true" : "false");
      } catch {
        /* non-fatal */
      }
    })();
  }, []);

  return { ...state, setLayer, hydrated };
}
