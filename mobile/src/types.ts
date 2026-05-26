// types.ts — shared TypeScript types for the mobile app.
//
// Mirror of the response shape from the FastAPI `/api/races` endpoint
// (see backend/app/routers/races.py). Kept narrow — only the fields the
// mobile UI actually reads today. When new fields are needed, widen here
// rather than scattering inline shapes.
//
// Why a hand-maintained mirror and not generated types: the backend isn't
// emitting an OpenAPI schema we consume, and the response is small + slow-
// changing. If/when we add openapi-typescript to CI, this file becomes
// the migration target.

/** Single course mark stored on a race. */
export type RaceMark = {
  name: string;
  lat: number;
  lon: number;
  /** Optional rounding side: "P" (port) or "S" (starboard). */
  rounding?: "P" | "S" | null;
};

/** Recorded passage of a mark during a sailed race. */
export type MarkPass = {
  mark_index: number;
  passed_at: string; // ISO timestamp
};

/** Race row as returned by GET /api/races and /api/races/{id}. */
export type Race = {
  id: string;
  name: string;
  /** "buoy" | "distance" | ... — see backend RaceMode. */
  mode: string;
  boat_class: string;
  marks: RaceMark[];
  /** Gun time. ISO string or null if unscheduled. */
  start_at: string | null;
  /** When the recorder actually began for this race. Null until raced. */
  started_at: string | null;
  /** When the recorder stopped / final mark detected. */
  ended_at: string | null;
  uses_spinnaker: boolean | null;
  user_id: string | null;
  created_at: string;
  updated_at: string;
  /** Populated when the race has been sailed. Empty/undefined otherwise. */
  mark_passes?: MarkPass[];
  /** True when post-processing produced viewable stats. */
  stats_available?: boolean;
};
