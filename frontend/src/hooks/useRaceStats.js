// useRaceStats — fetch GET /api/races/{id}/stats and expose the result.
//
// The endpoint returns stats + AI summary + wind summary in one
// payload (see backend/app/routers/race_stats.py). Three fetch states
// are exposed: loading / error / data.
//
// Auto-refetch:
//   When the response carries `summary_pending: true` it means the
//   Cloud Run postprocess job hasn't finished writing ai_summary yet.
//   We poll the endpoint every 8s up to 5 minutes, then back off. As
//   soon as `summary_pending` flips to false (or we get a non-null
//   ai_summary), polling stops.
//
// regenerate(): wraps POST /api/races/{id}/stats/regenerate (pro
// tier). Re-fires the postprocess job with --force on the backend.
// We immediately re-enable the polling loop so the new summary shows
// up as soon as it's written.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api";

const POLL_INTERVAL_MS = 8000;
const POLL_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
const STATS_TRACK_PATH = (id) => `/api/races/${id}/stats`;
const REGENERATE_PATH = (id) => `/api/races/${id}/stats/regenerate`;
const TRACK_PATH = (id) => `/api/races/${id}/track`;

export function useRaceStats(raceId) {
  const [data, setData] = useState(null);          // StatsResponse
  const [track, setTrack] = useState(null);        // [{lat, lon, ...}, ...]
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [regenerating, setRegenerating] = useState(false);

  // Polling state — kept in refs so an effect cleanup can stop them
  // without becoming part of the dep graph.
  const pollTimerRef = useRef(null);
  const pollDeadlineRef = useRef(0);

  const clearPoll = useCallback(() => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const fetchOnce = useCallback(async () => {
    if (!raceId) return null;
    return apiFetch(STATS_TRACK_PATH(raceId));
  }, [raceId]);

  const fetchTrack = useCallback(async () => {
    if (!raceId) return null;
    return apiFetch(TRACK_PATH(raceId));
  }, [raceId]);

  const refresh = useCallback(async () => {
    if (!raceId) return;
    setLoading(true);
    setError(null);
    try {
      // Parallel fetch — stats and the raw track for the map.
      const [stats, pts] = await Promise.all([fetchOnce(), fetchTrack()]);
      setData(stats);
      setTrack(pts || []);
      // Re-arm polling whenever a fresh fetch shows summary_pending.
      if (stats?.summary_pending) {
        pollDeadlineRef.current = Date.now() + POLL_TIMEOUT_MS;
        schedulePoll();
      }
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [raceId, fetchOnce, fetchTrack]);

  // schedulePoll schedules the NEXT poll attempt. It re-checks the
  // deadline so it self-terminates after 5 min.
  function schedulePoll() {
    clearPoll();
    if (Date.now() > pollDeadlineRef.current) return;
    pollTimerRef.current = setTimeout(async () => {
      try {
        const res = await fetchOnce();
        setData(res);
        if (res?.summary_pending && Date.now() < pollDeadlineRef.current) {
          schedulePoll();
        }
      } catch {
        // Swallow polling errors silently — UI still shows the last
        // good data. A real refresh() click can resurface a fresh
        // error.
      }
    }, POLL_INTERVAL_MS);
  }

  const regenerate = useCallback(async () => {
    if (!raceId) return;
    setRegenerating(true);
    setError(null);
    try {
      await apiFetch(REGENERATE_PATH(raceId), { method: "POST" });
      // The job is async; the new summary lands in the row once it
      // finishes. Treat as pending and let the poller pick it up.
      pollDeadlineRef.current = Date.now() + POLL_TIMEOUT_MS;
      schedulePoll();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setRegenerating(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [raceId]);

  // First load on raceId change.
  useEffect(() => {
    if (!raceId) {
      setData(null);
      setTrack(null);
      clearPoll();
      return;
    }
    refresh();
    return () => clearPoll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [raceId]);

  return {
    data,
    track,
    loading,
    error,
    regenerating,
    refresh,
    regenerate,
  };
}
