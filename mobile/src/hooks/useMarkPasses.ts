// useMarkPasses.ts — fetch + maintain the race's mark_passes list during
// a race. Powers the in-race MarkPassControls pill row.
//
// Source-of-truth handling:
//   * Authoritative passes live server-side, written by track_ingest (auto)
//     or by the manual-pass endpoint. This hook polls the race row on a
//     loose cadence to surface auto-detected passes that the recorder's
//     telemetry POSTs trigger.
//   * Manual passes go through `markManualPass()` which POSTs directly and
//     splices the response into local state immediately — no wait for the
//     next poll. Reduces "did my tap register?" anxiety.
//
// Polling cadence: 15 s while recording, 60 s otherwise. The telemetry
// flush interval is ~30 s so 15 s catches each batch's pass emissions
// within one cycle. Stopped recordings rarely change but a 60 s tick
// keeps the screen accurate if another device is sailing the same race.
//
// On error the hook surfaces it via `error` but keeps the last-good
// `passes` so the UI doesn't blink-empty during transient network
// failures (boat in a wifi dead zone).

import { useCallback, useEffect, useRef, useState } from "react";

import {
  getRace,
  recordManualMarkPass,
  type MarkPass,
} from "../api/races";

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
  /** Last fetch / mutation error, sticky until the next success. */
  error: string | null;
  /** Manually mark `markIndex` (and any unpassed marks before it) as
   *  passed. Returns the new list on success; throws on failure. */
  markManualPass: (
    markIndex: number,
    opts?: { lat?: number; lon?: number },
  ) => Promise<MarkPass[]>;
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
      // Race.mark_passes is optional on the wire — coerce to []
      const next = (race.mark_passes ?? []).slice().sort(
        (a, b) => a.mark_index - b.mark_index,
      );
      setPasses(next as MarkPass[]);
      setError(null);
      setLoading(false);
    } catch (e) {
      // Sticky error — keep the last-good passes visible so the user
      // doesn't lose the picture during a transient network blip.
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

  const markManualPass = useCallback(
    async (
      markIndex: number,
      opts: { lat?: number; lon?: number } = {},
    ): Promise<MarkPass[]> => {
      const id = raceIdRef.current;
      if (!id) throw new Error("No race selected");
      try {
        const resp = await recordManualMarkPass(id, markIndex, opts);
        const next = resp.mark_passes
          .slice()
          .sort((a, b) => a.mark_index - b.mark_index);
        setPasses(next);
        setError(null);
        return next;
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        throw e;
      }
    },
    [],
  );

  const refresh = useCallback(async () => {
    await fetchOnce();
  }, [fetchOnce]);

  return { passes, loading, error, markManualPass, refresh };
}
