// useRaceStats.ts — fetch + poll the post-race stats for the Review
// screen.
//
// The stats themselves (legs, speeds, distance) come back immediately on
// the first call. The AI recap is written asynchronously by the
// race-postprocess Cloud Run Job, so the endpoint returns
// `summary_pending: true` with `ai_summary: null` until the job lands.
// This hook polls while in that "generating" state so the AI Coach card
// fills in without the user having to leave and come back.
//
// Phases the screen renders against:
//   loading      — first fetch in flight, nothing yet.
//   generating   — stats present, AI summary still being produced (poll).
//   ready        — ai_summary present.
//   unavailable  — job finished but produced no summary (no wind / no key
//                  / track not scoreable). Stats still shown.
//   error        — fetch failed and we have nothing to show.

import { useCallback, useEffect, useRef, useState } from "react";

import { getRaceStats, type RaceStats } from "../api/raceStats";

const POLL_MS = 4_000;
// Stop auto-polling after ~80s so a permanently-pending race doesn't spin
// forever; the user can still pull-to-refresh.
const MAX_POLLS = 20;

export type RaceStatsPhase =
  | "loading"
  | "generating"
  | "ready"
  | "unavailable"
  | "error";

export type UseRaceStatsApi = {
  data: RaceStats | null;
  phase: RaceStatsPhase;
  error: string | null;
  /** Manual refresh (pull-to-refresh / Retry button). Resets the poll
   *  budget so a Retry after the cap can poll again. */
  refresh: () => Promise<void>;
};

function derivePhase(
  data: RaceStats | null,
  error: string | null,
): RaceStatsPhase {
  if (!data) return error ? "error" : "loading";
  if (data.ai_summary) return "ready";
  if (data.summary_pending) return "generating";
  return "unavailable";
}

export function useRaceStats(raceId: string | null): UseRaceStatsApi {
  const [data, setData] = useState<RaceStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollsRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const fetchOnce = useCallback(async () => {
    if (!raceId) return;
    try {
      const next = await getRaceStats(raceId);
      setData(next);
      setError(null);
      // Keep polling only while the summary is still being produced.
      clearTimer();
      const stillGenerating = !next.ai_summary && next.summary_pending;
      if (stillGenerating && pollsRef.current < MAX_POLLS) {
        pollsRef.current += 1;
        timerRef.current = setTimeout(() => void fetchOnce(), POLL_MS);
      }
    } catch (e) {
      // Keep the last-good data (sticky) so a transient blip mid-poll
      // doesn't blank a screen that already rendered.
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [raceId]);

  useEffect(() => {
    pollsRef.current = 0;
    setData(null);
    setError(null);
    clearTimer();
    if (raceId) void fetchOnce();
    return clearTimer;
  }, [raceId, fetchOnce]);

  const refresh = useCallback(async () => {
    pollsRef.current = 0; // re-arm the poll budget on a manual retry
    await fetchOnce();
  }, [fetchOnce]);

  return { data, phase: derivePhase(data, error), error, refresh };
}
