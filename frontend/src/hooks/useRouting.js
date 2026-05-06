// frontend/src/hooks/useRouting.js
//
// useRouting — wraps POST /api/routing/compute and exposes the displayed
// route + meta as state.
//
// Invoked from the active-race overlay's "Compute Route" button. On
// success the hook holds the GeoJSON Feature plus diagnostic metadata
// (total time, tack count, region, forecast quality). MapView reads
// `route` and pushes it to a Mapbox geojson source so the magenta line
// renders.
//
// applyAlternative(feature) lets the SSE notifications path swap in a
// better route without going through compute() again — the recompute
// worker already did that calculation server-side and published the
// result. The Feature carries its own meta inside `properties`, which
// we extract for the status badge.
//
// Cache: server-side keyed by (race_id, cycle, race_start, safety_factor)
// for 1h. The hook itself doesn't cache — re-clicking Compute Route
// always issues a new POST.

import { useCallback, useState } from "react";
import { apiFetch } from "../api";

export function useRouting(raceId) {
  const [route, setRoute] = useState(null); // GeoJSON Feature or null
  const [meta, setMeta] = useState(null);   // { total_minutes, tack_count, ... }
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

  // Apply an alternative route delivered via the SSE notifications
  // stream. The recompute worker publishes the same Feature shape that
  // /api/routing/compute returns, with a meta-flavoured properties
  // object — pull what we need to keep the status badge accurate.
  const applyAlternative = useCallback((feature) => {
    if (!feature || feature.type !== "Feature") return;
    const props = feature.properties || {};
    setRoute(feature);
    setMeta({
      total_minutes: props.total_minutes ?? 0,
      tack_count: props.tack_count ?? 0,
      reached: props.reached ?? true,
      iterations: props.iterations ?? 0,
      nodes_explored: props.nodes_explored ?? 0,
      region: props.region ?? "",
      forecast_quality: props.forecast_quality ?? "",
      polar: props.polar ?? "",
      cached: false,
    });
    setError(null);
  }, []);

  return { route, meta, loading, error, compute, clear, applyAlternative };
}
