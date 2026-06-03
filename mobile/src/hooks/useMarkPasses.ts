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
// 2026-06-03 A1 fix — UNION-MERGE rather than replace.
//   Bug we're fixing: a manual pass would show green, then revert within
//   seconds. Two simultaneous causes:
//     (a) Cloud Run read-after-write race — the 15 s poll lands on an
//         instance that hasn't seen the manual-pass write yet, returns
//         `mark_passes: []`, the old reducer overwrites local state.
//     (b) `race.mark_passes` is occasionally wire-encoded as a JSON
//         string instead of an array (a backend serialisation bug,
//         flagged separately). The old reducer treated that as "empty"
//         and again wiped the UI.
//   Fix: parse defensively in api/races.ts before this hook sees it,
//   and union-merge here so a locally-known pass survives a poll where
//   the server hasn't caught up. Server is still authoritative for
//   marks it knows about — when both sides have the same mark_index we
//   keep the server row (it has the canonical ts/lat/lon written by
//   the detector or the manual-pass endpoint).
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

/**
 * Merge a freshly-polled server list into the locally-known list,
 * keyed on mark_index.
 *
 * Rules:
 *   - If both sides have a pass for mark_index N, keep the SERVER row
 *     (canonical ts/lat/lon, possibly upgraded from manual→auto or
 *     vice-versa).
 *   - If only LOCAL has it, keep the local row. This is the case the
 *     bug fix is about: a manual tap that hasn't propagated to the
 *     instance answering this poll yet.
 *   - If only SERVER has it, take it (auto-detected pass we didn't
 *     know about).
 *   - Sort ascending by mark_index for stable rendering.
 */
export function mergePasses(local: MarkPass[], server: MarkPass[]): MarkPass[] {
  const byIndex = new Map<number, MarkPass>();
  for (const p of local) byIndex.set(p.mark_index, p);
  // Server wins on overlap — overwrite any local entry with the same index.
  for (const p of server) byIndex.set(p.mark_index, p);
  return Array.from(byIndex.values()).sort(
    (a, b) => a.mark_index - b.mark_index,
  );
}

export function useMarkPasses({ raceId, recording }: Options): UseMarkPassesApi {
  const [passes, setPasses] = useState<MarkPass[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Mirror raceId into a ref so the interval callback always sees the
  // latest id without a re-mount of the effect on every change.
  const raceIdRef = useRef(raceId);
  raceIdRef.current = raceId;

  // Mirror the latest `passes` into a ref so fetchOnce can read it
  // without re-creating itself on every render (which would tear down
  // the polling interval). The merge step needs the local list.
  const passesRef = useRef<MarkPass[]>([]);
  passesRef.current = passes;

  const fetchOnce = useCallback(async () => {
    const id = raceIdRef.current;
    if (!id) {
      setPasses([]);
      passesRef.current = [];
      setLoading(false);
      return;
    }
    try {
      const race = await getRace(id);
      // api/races.ts::getRace normalises the wire shape so this is
      // already a real array (or undefined). No need to re-parse.
      const serverPasses = (race.mark_passes ?? []) as MarkPass[];
      const merged = mergePasses(passesRef.current, serverPasses);
      setPasses(merged);
      passesRef.current = merged;
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
      passesRef.current = [];
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
        // The manual-pass response is authoritative — the server just
        // wrote it, so it has the full canonical list. Replace local
        // wholesale (sorted) and seed passesRef so the next poll merges
        // against the right baseline.
        const next = resp.mark_passes
          .slice()
          .sort((a, b) => a.mark_index - b.mark_index);
        setPasses(next);
        passesRef.current = next;
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
