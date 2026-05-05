// useRouting — wraps POST /api/routing/compute.
//
// Invoked from the active-race overlay's "Compute Route" button. On success
// the hook holds the GeoJSON Feature plus diagnostic metadata (total time,
// tack count, region, wind reference time). MapView reads `route` and
// pushes it to a Mapbox geojson source so the magenta line renders.
//
// Cache: server-side keyed by (race_id, hrrr_reference_time) for 1h. The
// hook itself doesn't cache — re-clicking Compute Route always issues a
// new POST, which lets the user force a fresh calc on demand. Server
// returns the cached result if the wind hasn't rotated.
//
// Errors surface in `error`; the caller can display next to the button.

import { useCallback, useState } from "react";
import { apiFetch } from "../api";

export function useRouting(raceId) {
  const [route, setRoute] = useState(null);  // GeoJSON Feature or null
  const [meta, setMeta] = useState(null);    // { total_minutes, tack_count, ... }
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const compute = useCallback(async () => {
    if (!raceId) {
      setError("No active race");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/routing/compute", {
        method: "POST",
        body: { race_id: raceId },
      });
      setRoute(res.route);
      setMeta(res.meta);
    } catch (e) {
      setError(e.message || String(e));
      setRoute(null);
      setMeta(null);
    } finally {
      setLoading(false);
    }
  }, [raceId]);

  const clear = useCallback(() => {
    setRoute(null);
    setMeta(null);
    setError(null);
  }, []);

  return { route, meta, loading, error, compute, clear };
}
