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
//   generating   — stats present, AI summary still being produced (poll
//                  every 20 s, up to 10 min).
//   ready        — ai_summary present.
//   unavailable  — poll budget spent, or the job produced no summary
//                  (no wind / no key / track not scoreable). Stats still
//                  shown; Retry re-arms the budget.
//   error        — fetch failed and we have nothing to show.

import { useCallback, useEffect, useRef, useState } from "react";

import { getRaceStats, type RaceStats } from "../api/raceStats";

const POLL_MS = 20_000;
// Stop auto-polling after 10 min (30 polls × 20 s) so a permanently-
// pending race doesn't spin forever — the postprocess job normally lands
// well inside that window. After the budget the phase flips to
// "unavailable" and the Retry button re-arms it.
const MAX_POLLS = 30;

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
  exhausted: boolean,
): RaceStatsPhase {
  if (!data) return error ? "error" : "loading";
  if (data.ai_summary) return "ready";
  // summary_pending stays true server-side for as long as a summary is
  // absent (see race_stats.py), so the poll budget is what keeps a race
  // that will never get one from showing a spinner forever.
  if (data.summary_pending && !exhausted) return "generating";
  return "unavailable";
}

export function useRaceStats(raceId: string | null): UseRaceStatsApi {
  const [data, setData] = useState<RaceStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exhausted, setExhausted] = useState(false);
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
      } else if (stillGenerating) {
        // Budget spent without a summary — stop the spinner (the phase
        // flips to "unavailable"); Retry re-arms the budget.
        setExhausted(true);
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
    setExhausted(false);
    clearTimer();
    if (raceId) void fetchOnce();
    return clearTimer;
  }, [raceId, fetchOnce]);

  const refresh = useCallback(async () => {
    pollsRef.current = 0; // re-arm the poll budget on a manual retry
    setExhausted(false);
    await fetchOnce();
  }, [fetchOnce]);

  return { data, phase: derivePhase(data, error, exhausted), error, refresh };
}
