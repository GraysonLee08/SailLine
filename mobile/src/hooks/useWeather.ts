// hooks/useWeather.ts — fetches the regridded wind grid for a region.
//
// Mirrors the read-then-refetch-on-cycle-change behaviour of
// frontend/src/hooks/useWeather.js but simpler — mobile UX doesn't yet
// have the "background refresh every 6h" polling the web has, because
// the bottom-sheet UI is short-lived per race. The user can pull-to-
// refresh the race list to also re-trigger weather load.
//
// If the region changes (user picks a race in another base region), the
// hook discards the in-memory grid and refetches.

import { useEffect, useState } from "react";

import { getWeather, type WindGrid } from "../api/weather";

export function useWeather(region: string | null) {
  const [grid, setGrid] = useState<WindGrid | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!region) {
      setGrid(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    // eslint-disable-next-line no-console
    console.log("[useWeather] fetching region:", region);
    (async () => {
      try {
        const data = await getWeather(region);
        if (cancelled) return;
        // eslint-disable-next-line no-console
        console.log("[useWeather] grid loaded:", {
          source: data?.source,
          valid_time: data?.valid_time,
          lats: data?.lats?.length,
          lons: data?.lons?.length,
          u_rows: data?.u?.length,
          u_cols: data?.u?.[0]?.length,
        });
        setGrid(data);
      } catch (e) {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : String(e);
        // eslint-disable-next-line no-console
        console.error("[useWeather] fetch failed:", msg);
        setError(msg);
        setGrid(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [region]);

  return { grid, loading, error };
}
