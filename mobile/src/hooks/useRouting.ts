// hooks/useRouting.ts — wraps POST /api/routing/compute.
//
// Port of frontend/src/hooks/useRouting.js. Differences:
//   - Discriminated-union result handling for the 425 case (see api/routing.ts).
//   - applyAlternative receives the SSE payload's `route` Feature directly
//     and rehydrates meta from its properties — matches the web version's
//     contract so the same backend payload format works in both clients.

import { useCallback, useState } from "react";

import { computeRoute } from "../api/routing";
import type {
  RouteFeature,
  RouteMeta,
} from "../api/routing";

type Pending = {
  detail: string;
  availableAt: string;
  hoursUntilAvailable: number;
};

export function useRouting(raceId: string | null) {
  const [route, setRoute] = useState<RouteFeature | null>(null);
  const [meta, setMeta] = useState<RouteMeta | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const compute = useCallback(async () => {
    if (!raceId) {
      setError("No active race");
      return;
    }
    setLoading(true);
    setError(null);
    setPending(null);
    try {
      const result = await computeRoute(raceId);
      if (result.kind === "pending") {
        setPending({
          detail: result.detail,
          availableAt: result.availableAt,
          hoursUntilAvailable: result.hoursUntilAvailable,
        });
        setRoute(null);
        setMeta(null);
      } else {
        setRoute(result.route);
        setMeta(result.meta);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRoute(null);
      setMeta(null);
    } finally {
      setLoading(false);
    }
  }, [raceId]);

  const clear = useCallback(() => {
    setRoute(null);
    setMeta(null);
    setPending(null);
    setError(null);
  }, []);

  /**
   * Adopt an alternative route delivered via SSE. The recompute worker
   * publishes the same Feature shape as POST /compute, with meta-like
   * fields under `properties`. Rehydrate the meta tokens we care about
   * so the existing display logic doesn't need a special case.
   */
  const applyAlternative = useCallback((feature: RouteFeature) => {
    if (!feature || feature.type !== "Feature") return;
    const p = (feature.properties ?? {}) as Record<string, unknown>;
    const numOr = (v: unknown, fb: number): number =>
      typeof v === "number" && Number.isFinite(v) ? v : fb;
    const strOr = (v: unknown, fb: string): string =>
      typeof v === "string" ? v : fb;
    setRoute(feature);
    setMeta({
      total_minutes: numOr(p.total_minutes, 0),
      tack_count: numOr(p.tack_count, 0),
      reached: typeof p.reached === "boolean" ? p.reached : true,
      iterations: numOr(p.iterations, 0),
      nodes_explored: numOr(p.nodes_explored, 0),
      legs: numOr(p.legs, 0),
      region: strOr(p.region, ""),
      venue: typeof p.venue === "string" ? p.venue : null,
      forecast_quality: strOr(p.forecast_quality, ""),
      race_start: typeof p.race_start === "string" ? p.race_start : null,
      polar: strOr(p.polar, ""),
      boat_class: strOr(p.boat_class, ""),
      draft_m: numOr(p.draft_m, 0),
      min_depth_m: numOr(p.min_depth_m, 0),
      cached: false,
      max_tws_kt: typeof p.max_tws_kt === "number" ? p.max_tws_kt : null,
      polar_margin: numOr(p.polar_margin, 1.0),
      hs_m: numOr(p.hs_m, 0),
      density_factor: numOr(p.density_factor, 1.0),
      currents_quality:
        typeof p.currents_quality === "string" ? p.currents_quality : null,
      start_wind_dir_deg:
        typeof p.start_wind_dir_deg === "number" ? p.start_wind_dir_deg : null,
      start_wind_speed_kt:
        typeof p.start_wind_speed_kt === "number"
          ? p.start_wind_speed_kt
          : null,
    });
    setError(null);
    setPending(null);
  }, []);

  return {
    route,
    meta,
    pending,
    loading,
    error,
    compute,
    clear,
    applyAlternative,
  };
}
