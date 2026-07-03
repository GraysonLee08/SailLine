// api/raceStats.ts — typed wrapper for the post-race stats endpoint.
//
// GET /api/races/{id}/stats returns the recomputed stats (legs, speeds,
// corrected time) PLUS the persisted postprocess outputs written by the
// race-postprocess Cloud Run Job: the AI recap, wind snapshot meta, heel
// rollup, and Target-Actual performance. `summary_pending` tells the
// caller whether the job is still expected to fill in the AI summary
// (poll) or has finished without one (show stats only).
//
// Shapes mirror backend/app/routers/race_stats.py::StatsResponse. Keep
// them in sync if that model changes.

import { apiFetch } from "../api";

export type Leg = {
  leg_index: number;
  from_label: string;
  to_label: string;
  start_ts: string;
  end_ts: string;
  elapsed_s: number;
  distance_m: number;
  avg_sog_kt: number;
};

export type Stats = {
  point_count: number;
  started_at: string;
  ended_at: string;
  elapsed_s: number;
  moving_s: number;
  stopped_s: number;
  distance_m: number;
  avg_sog_kt: number;
  avg_moving_sog_kt: number;
  max_sog_kt: number;
  legs: Leg[];
  corrected_time_s?: number | null;
  corrected_using?: string | null;
  rating_seconds_per_mile?: number | null;
};

export type CostFinding = {
  tag: "EXECUTION" | "DECISION";
  text: string;
  cost_s?: number | null;
};

export type Playbook = {
  signature?: Record<string, unknown> | null;
  signature_text?: string | null;
  directives: string[];
};

export type AiSummary = {
  // v4 analysis shape (prompt_version >= 4)
  summary?: string;
  what_worked?: string[];
  what_cost?: CostFinding[];
  total_identifiable_loss_s?: number | null;
  playbook?: Playbook | null;
  analysis?: Record<string, unknown> | null;
  // Legacy v3 shape — present until the race is regenerated under v4
  recap?: string;
  tips?: string[];
  model?: string | null;
  prompt_version?: number | null;
  generated_at?: string | null;
};

export type WindMeta = {
  source?: string | null;
  mean_speed_kt?: number | null;
  max_speed_kt?: number | null;
  mean_dir_deg?: number | null;
  dir_range_deg?: number | null;
};

export type HeelSummary = {
  sample_count: number;
  max_heel_abs_deg: number;
  avg_heel_abs_deg: number;
  pct_time_heeled_gt_10: number;
  pct_time_heeled_gt_20: number;
};

export type PerformanceSummary = {
  sample_count: number;
  avg_speed_ratio?: number | null;
  avg_vmg_efficiency?: number | null;
  pct_time_on_target: number;
  avg_target_kts?: number | null;
  avg_actual_kts?: number | null;
};

export type RaceStats = {
  race_id: string;
  name?: string | null;
  boat_class?: string | null;
  start_at?: string | null;
  mode?: string | null;
  uses_spinnaker: boolean;
  marks: Array<{ lat: number; lon: number; name?: string }>;
  stats?: Stats | null;
  ai_summary?: AiSummary | null;
  wind?: WindMeta | null;
  heel_summary?: HeelSummary | null;
  performance_summary?: PerformanceSummary | null;
  summary_pending: boolean;
};

/** Fetch computed stats + AI summary for a finished race. Throws on 404
 *  (race not found / no access) or 425-style "no track yet" cases the
 *  endpoint surfaces as errors. */
export async function getRaceStats(raceId: string): Promise<RaceStats> {
  const data = await apiFetch<RaceStats>(`/api/races/${raceId}/stats`);
  if (!data) throw new Error(`Stats for race ${raceId} returned no body`);
  return data;
}
