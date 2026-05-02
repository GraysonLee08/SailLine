import { useEffect, useRef, useState } from "react";

// In production, paths are relative — Firebase Hosting rewrites /api/** to
// the sailline-api Cloud Run service (same-origin, no CORS).
// In local dev, set VITE_API_URL=http://localhost:8080 in .env.local.
const API_URL = import.meta.env.VITE_API_URL || "";

const REFRESH_MS = 5 * 60 * 1000; // refetch cadence — matches Cache-Control: max-age=300
const TICK_MS = 60 * 1000;        // re-render cadence to keep ageMinutes fresh

/**
 * Fetch the cached wind grid for a region. Caches the ETag in memory and
 * sends If-None-Match on refetch — a 304 means the cycle hasn't rotated
 * and the previously parsed payload stays in state.
 *
 * Pass `region = null` (or undefined/empty) to disable fetching — the
 * hook returns `data: null, loading: false`. This is the supported pattern
 * for the venue overlay layer in MapView, which only wants to fetch when
 * the user is zoomed into a venue. React doesn't allow conditional hooks,
 * so we toggle behavior via the argument instead.
 *
 * @param {string|null} region  e.g. "conus", "sf_bay", or null to skip
 * @param {"hrrr"|"gfs"} source
 * @returns {{
 *   data: object|null,         // full payload: { lats, lons, u, v, shape, bbox, ... }
 *   referenceTime: Date|null,  // when the model was run
 *   validTime: Date|null,      // forecast hour the grid represents
 *   ageMinutes: number|null,   // minutes since validTime, recomputed each render
 *   loading: boolean,
 *   error: Error|null,
 * }}
 */
export function useWeather(region, source = "hrrr") {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(Boolean(region));
  const [error, setError] = useState(null);

  // ETag survives refetches without causing re-renders.
  const etagRef = useRef(null);
  // Tick forces a re-render every minute so ageMinutes updates.
  const [, setTick] = useState(0);

  useEffect(() => {
    // No region → clear state and bail. This handles the "user zoomed
    // out of a venue" transition cleanly: the venue layer's data goes
    // away immediately rather than going stale.
    if (!region) {
      setData(null);
      setLoading(false);
      setError(null);
      etagRef.current = null;
      return;
    }

    let cancelled = false;
    etagRef.current = null; // new (region, source) → drop the old cycle's ETag
    setLoading(true);

    async function load() {
      try {
        const headers = {};
        if (etagRef.current) headers["If-None-Match"] = etagRef.current;

        const url =
          `${API_URL}/api/weather` +
          `?region=${encodeURIComponent(region)}` +
          `&source=${encodeURIComponent(source)}`;

        const res = await fetch(url, { headers });
        if (cancelled) return;

        if (res.status === 304) {
          // Cycle hasn't rotated — keep existing data, just clear loading.
          setLoading(false);
          setError(null);
          return;
        }
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          throw new Error(`API ${res.status}: ${text || res.statusText}`);
        }

        // Browser transparently inflates Content-Encoding: gzip.
        const payload = await res.json();
        const newEtag = res.headers.get("ETag");
        if (newEtag) etagRef.current = newEtag;

        setData(payload);
        setLoading(false);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err);
        setLoading(false);
      }
    }

    load();
    const refresh = setInterval(load, REFRESH_MS);
    const tick = setInterval(() => setTick((t) => t + 1), TICK_MS);

    return () => {
      cancelled = true;
      clearInterval(refresh);
      clearInterval(tick);
    };
  }, [region, source]);

  const referenceTime = data ? new Date(data.reference_time) : null;
  const validTime = data ? new Date(data.valid_time) : null;
  const ageMinutes = validTime
    ? Math.round((Date.now() - validTime.getTime()) / 60_000)
    : null;

  return { data, referenceTime, validTime, ageMinutes, loading, error };
}
