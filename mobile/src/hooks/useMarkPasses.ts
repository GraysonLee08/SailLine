// useMarkPasses.ts — fetch + surface the race's auto-detected mark_passes
// during a race. Powers the missed-mark notifier's "next expected mark"
// logic (and, post-race, anything that reads the pass list).
//
// Source of truth: the SERVER, always. Passes are written exclusively by
// the backend mark-rounding detector on track ingest. There is no client
// writer — the manual-pass path (markManualPass / recordManualMarkPass /
// POST /mark-passes) was removed 2026-06-08 because a racer doesn't touch
// the phone mid-race, and the dual-writer (manual + auto) arrangement
// produced an off-by-one in mark_index. With one writer the detector's
// index == its position and the list is always consistent, so this hook
// just polls and reflects what the server has.
//
// Polling cadence: 15 s while recording, 60 s otherwise. The telemetry
// flush interval is ~30 s so 15 s catches each batch's pass emissions
// within one cycle. Stopped recordings rarely change but a 60 s tick
// keeps the screen accurate if another device is sailing the same race.
//
// On error the hook surfaces it via `error` but keeps the last-good
// `passes` so consumers don't blink-empty during transient network
// failures (boat in a wifi dead zone).

import { useCallback, useEffect, useRef, useState } from "react";

import { getRace, type MarkPass } from "../api/races";

const POLL_RECORDING_MS = 15_000;
const POLL_IDLE_MS = 60_000;

type Options = {
  /** Skip work when no race is selected (or the race id changes). */
  raceId: string | null;
  /** Drives the poll cadence. */
  recording: boolean;
};

export type UseMarkPassesApi = {
  /** Current list of passes, sorted by mark_index ascending. */
  passes: MarkPass[];
  /** True until the first fetch completes. */
  loading: boolean;
  /** Last fetch error, sticky until the next success. */
  error: string | null;
  /** Force an immediate refresh (e.g. after the user pulls down). */
  refresh: () => Promise<void>;
};

export function useMarkPasses({ raceId, recording }: Options): UseMarkPassesApi {
  const [passes, setPasses] = useState<MarkPass[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Mirror raceId into a ref so the interval callback always sees the
  // latest id without a re-mount of the effect on every change.
  const raceIdRef = useRef(raceId);
  raceIdRef.current = raceId;

  const fetchOnce = useCallback(async () => {
    const id = raceIdRef.current;
    if (!id) {
      setPasses([]);
      setLoading(false);
      return;
    }
    try {
      const race = await getRace(id);
      // api/races.ts::getRace normalises the wire shape so this is
      // already a real array (or undefined). Server is authoritative —
      // sort by mark_index for stable rendering and reflect it directly.
      const serverPasses = ((race.mark_passes ?? []) as MarkPass[])
        .slice()
        .sort((a, b) => a.mark_index - b.mark_index);
      setPasses(serverPasses);
      setError(null);
      setLoading(false);
    } catch (e) {
      // Sticky error — keep the last-good passes visible so consumers
      // don't lose the picture during a transient network blip.
      setError(e instanceof Error ? e.message : String(e));
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!raceId) {
      setPasses([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    void fetchOnce();
    const intervalMs = recording ? POLL_RECORDING_MS : POLL_IDLE_MS;
    const timer = setInterval(() => void fetchOnce(), intervalMs);
    return () => clearInterval(timer);
  }, [raceId, recording, fetchOnce]);

  const refresh = useCallback(async () => {
    await fetchOnce();
  }, [fetchOnce]);

  return { passes, loading, error, refresh };
}
