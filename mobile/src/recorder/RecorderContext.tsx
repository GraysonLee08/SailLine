// RecorderContext.tsx — hoists useTrackRecorder to a layout-level lifetime.
//
// The recorder hook spawns a long-lived foreground service (Transistorsoft)
// and an interval timer for the flush loop. Both must outlive any single
// screen — the user may navigate from Map → Recording → Map again
// mid-race, and the recorder must keep running.
//
// Owns one additional piece of cross-screen state: the currently selected
// race. The race picker writes it; the recording screen reads it. Stored
// here so navigating to /recording doesn't need to round-trip the race
// payload through expo-router's query params (which serialize as strings
// and would force re-parse on every render).

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

import { useAuth } from "../auth/AuthContext";
import { requestBatteryOptimizationExemption } from "./backgroundGeolocation";
import { startTokenRefresh } from "./tokenRefresh";
import { useTrackRecorder } from "./useTrackRecorder";
import type { LocalPoint } from "./backgroundGeolocation";
import type { UploadStatus } from "./uploadStatus";
import type { Race } from "../types";

type RecorderApi = {
  recording: boolean;
  error: string | null;
  points: ReadonlyArray<LocalPoint>;
  queueLength: number;
  lastPoint: LocalPoint | null;
  uploadStatus: UploadStatus;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  flushNow: () => Promise<void>;
};

type RecorderCtx = {
  /** Currently selected race; null when the user is browsing. */
  selectedRace: Race | null;
  /** Replace the selection. Blocked while recording; returns false if so. */
  setSelectedRace: (race: Race | null) => boolean;
  recorder: RecorderApi;
  /** Wraps recorder.start with the OS battery prompt nudge. */
  startRecording: () => Promise<void>;
};

const Ctx = createContext<RecorderCtx | null>(null);

export function RecorderProvider({ children }: { children: ReactNode }) {
  const [selectedRace, setSelectedRaceState] = useState<Race | null>(null);

  // Recorder lifetime spans the signed-in session via this provider's
  // mount lifetime. raceId is sourced from selectedRace; the hook itself
  // refuses to start when raceId is null (see useTrackRecorder.start).
  const recorder = useTrackRecorder(selectedRace?.id ?? null);

  // Block selection changes while recording so the user can't strand the
  // recorder pointing at a stale (or null) raceId. Returns false to let
  // the caller surface a "stop first" toast if they want.
  const setSelectedRace = useCallback(
    (race: Race | null) => {
      if (recorder.recording) return false;
      setSelectedRaceState(race);
      return true;
    },
    [recorder.recording],
  );

  const startRecording = useCallback(async () => {
    await requestBatteryOptimizationExemption();
    await recorder.start();
  }, [recorder]);

  // Defensive: if the recorder ever finds itself recording with no
  // selectedRace (shouldn't happen via UI, but the hook would refuse),
  // there's nothing to do — just log via dev console.
  useEffect(() => {
    if (recorder.recording && !selectedRace) {
      // eslint-disable-next-line no-console
      console.warn("[RecorderContext] recording with no selected race");
    }
  }, [recorder.recording, selectedRace]);

  // ── Phase 4 — keep native HTTP authed ──────────────────────────────
  //
  // Run only when there's a signed-in user. The token-refresh handlers
  // call auth.currentUser themselves, so the bare existence of a user
  // is sufficient to start them — they handle the moment-to-moment
  // refresh on AppState / onIdTokenChanged / interval ticks.
  //
  // No-op in JS-uploader mode: setAuthHeader is best-effort and the
  // plugin's http config block is absent when the flag is off, so the
  // header push is wasted work but not harmful. Phase 5 will remove
  // this branch once native uploader is the only code path.
  const { user } = useAuth();
  useEffect(() => {
    if (!user) return;
    return startTokenRefresh();
  }, [user]);

  const value = useMemo<RecorderCtx>(
    () => ({ selectedRace, setSelectedRace, recorder, startRecording }),
    [selectedRace, setSelectedRace, recorder, startRecording],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRecorder(): RecorderCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useRecorder must be used inside <RecorderProvider>");
  return v;
}
