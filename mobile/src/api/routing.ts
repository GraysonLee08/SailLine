// api/routing.ts — typed wrapper for POST /api/routing/compute.
//
// Mirrors the backend contract documented in backend/app/routers/routing.py.
// Surfaces the HTTP 425 (Too Early) case as a discriminated "pending"
// result instead of throwing, so the calling hook can show a friendly
// "forecast not out yet — back in 2h 14m" message and schedule a retry.

import { apiFetch } from "../api";

export type RouteMeta = {
  total_minutes: number;
  tack_count: number;
  reached: boolean;
  iterations: number;
  nodes_explored: number;
  legs: number;
  region: string;
  venue: string | null;
  forecast_quality: string;
  race_start: string | null;
  polar: string;
  boat_class: string;
  draft_m: number;
  min_depth_m: number;
  cached: boolean;
  max_tws_kt: number | null;
  polar_margin: number;
  hs_m: number;
  density_factor: number;
  currents_quality: string | null;
  start_wind_dir_deg: number | null;
  start_wind_speed_kt: number | null;
};

/** GeoJSON LineString Feature with route metadata in properties. */
export type RouteFeature = {
  type: "Feature";
  geometry: { type: "LineString"; coordinates: number[][] };
  properties: Record<string, unknown>;
};

export type ComputeRouteSuccess = {
  kind: "ok";
  route: RouteFeature;
  meta: RouteMeta;
};

export type ComputeRoutePending = {
  kind: "pending";
  detail: string;
  availableAt: string;     // ISO string
  hoursUntilAvailable: number;
};

export type ComputeRouteResult = ComputeRouteSuccess | ComputeRoutePending;

export type ComputeRouteOptions = {
  safety_factor?: number;
  duration_hours?: number;
  max_tws_kt?: number | null;
  polar_margin?: number;
  hs_m?: number;
  density_factor?: number;
};

/**
 * Compute (or fetch the cached) optimal route for a race.
 *
 * Returns a discriminated union — caller switches on `kind`. The 425 path
 * is a normal outcome (forecast not yet published by NOAA), NOT an error.
 * Any other non-2xx response throws via apiFetch's existing contract.
 */
export async function computeRoute(
  raceId: string,
  options: ComputeRouteOptions = {},
): Promise<ComputeRouteResult> {
  try {
    const data = await apiFetch<{ route: RouteFeature; meta: RouteMeta }>(
      "/api/routing/compute",
      { method: "POST", body: { race_id: raceId, ...options } },
    );
    if (!data) throw new Error("compute_route returned no body");
    return { kind: "ok", route: data.route, meta: data.meta };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    // apiFetch throws `API 425: <body>` — try to parse it back out. The
    // 425 body is JSON: { detail, available_at, hours_until_available }.
    const m = /^API 425:\s*(.+)$/s.exec(msg);
    if (m) {
      try {
        const parsed = JSON.parse(m[1]);
        const inner = parsed?.detail ?? parsed;
        return {
          kind: "pending",
          detail: typeof inner === "string" ? inner : "Forecast not yet available",
          availableAt: parsed?.available_at ?? parsed?.detail?.available_at ?? "",
          hoursUntilAvailable:
            Number(
              parsed?.hours_until_available ??
                parsed?.detail?.hours_until_available ??
                0,
            ) || 0,
        };
      } catch {
        // Fall through — surface as a regular error.
      }
    }
    throw e;
  }
}
