// api/routing.ts — typed wrapper for POST /api/routing/compute.
//
// Mirrors the backend contract documented in backend/app/routers/routing.py.
// Surfaces the HTTP 425 (Too Early) case as a discriminated "pending"
// result instead of throwing, so the calling hook can show a friendly
// "forecast not out yet — back in 2h 14m" message and schedule a retry.
//
// All other 4xx responses are unwrapped: FastAPI wraps errors as
// `{"detail": "..."}`, and apiFetch throws `Error("API 400: <body>")`.
// We parse out the `detail` string so the UI shows the human-readable
// reason ("race must have at least 2 marks (start + finish)") instead
// of the full envelope. The "<2 marks" case in particular gets a
// friendlier rewrite — that error is reachable from the happy path
// (user picks a 0-or-1-mark race and taps Compute) and the raw
// backend wording leaks an implementation detail.

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

/** Match the `API <status>: <body>` envelope thrown by apiFetch. */
const API_ERROR_RE = /^API (\d{3}):\s*(.+)$/s;

/**
 * Translate a backend `detail` string into a user-facing message.
 * Keeps the original detail as the default — callers see whatever the
 * server said unless we've explicitly rewritten it.
 */
function friendlyDetail(detail: string): string {
  if (/at least 2 marks/i.test(detail)) {
    return "This race needs at least 2 marks (start + finish). Open the race to add marks before computing a route.";
  }
  return detail;
}

/**
 * Compute (or fetch the cached) optimal route for a race.
 *
 * Returns a discriminated union — caller switches on `kind`. The 425 path
 * is a normal outcome (forecast not yet published by NOAA), NOT an error.
 * Other non-2xx responses throw with the backend's `detail` string
 * (translated to a friendlier wording where applicable) instead of the
 * raw `API <status>: {...}` envelope.
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
    const m = API_ERROR_RE.exec(msg);
    if (!m) throw e;
    const status = Number(m[1]);
    const rawBody = m[2];

    // Try to parse the body as a FastAPI error envelope: {"detail": ...}.
    // detail can be a string (most cases) or an object (the 425 case).
    let parsedDetail: unknown = rawBody;
    try {
      const parsed = JSON.parse(rawBody);
      parsedDetail = parsed?.detail ?? parsed;
    } catch {
      /* keep rawBody as the detail */
    }

    // 425 Too Early → "pending" result, not an error.
    if (status === 425) {
      const inner =
        typeof parsedDetail === "object" && parsedDetail !== null
          ? (parsedDetail as Record<string, unknown>)
          : null;
      return {
        kind: "pending",
        detail:
          typeof inner?.detail === "string"
            ? (inner.detail as string)
            : typeof parsedDetail === "string"
              ? parsedDetail
              : "Forecast not yet available",
        availableAt:
          typeof inner?.available_at === "string"
            ? (inner.available_at as string)
            : "",
        hoursUntilAvailable:
          typeof inner?.hours_until_available === "number"
            ? (inner.hours_until_available as number)
            : 0,
      };
    }

    // Everything else → throw with the friendlier detail string.
    const detailStr =
      typeof parsedDetail === "string"
        ? parsedDetail
        : JSON.stringify(parsedDetail);
    throw new Error(friendlyDetail(detailStr));
  }
}
