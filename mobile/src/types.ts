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
  /** Race-book description; optional. */
  description?: string | null;
  /** "Leave Mark to" — drives the v4 gate detector's rounding ray
   *  (2026-07-02). Optional: older races lack it and fall back to CPA
   *  detection server-side. NOTE: an earlier declaration here said
   *  "P" | "S" but nothing ever read or wrote that shape — the wire
   *  contract is the full words, matching backend races.Mark. */
  rounding?: "port" | "starboard" | null;
};

/** Recorded passage of a mark during a sailed race. Mirrors the
 *  backend's `MarkPassOut` model + the JSONB shape on
 *  `race_sessions.mark_passes`.
 *
 *  v3 (2026-05-30): added `source` so manual passes (via the in-race
 *  Pass button or watch notification) can be distinguished from
 *  auto-detected ones. Older rows lack the field; treat absent as
 *  "auto" for back-compat.
 */
export type MarkPass = {
  mark_index: number;
  ts: string; // ISO timestamp (closest-point-of-approach)
  lat: number;
  lon: number;
  source?: "auto" | "manual";
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
  /** v4 (2026-07-02): manual start/finish line bearing (degrees true).
   *  Null = derive from forecast wind at gun time. Read-only on mobile
   *  today — set from the web race editor. */
  start_line_bearing_override?: number | null;
};
