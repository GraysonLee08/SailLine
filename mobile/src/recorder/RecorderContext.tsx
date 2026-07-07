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
//
// 2026-07-05 — relaunch reconciliation. Recording sessions now survive a
// process kill / reboot (stopOnTerminate:false + startOnBoot:true in
// native-uploader mode; see backgroundGeolocation.ts). That means the JS
// tree can boot ABOVE an already-running native service. On sign-in this
// provider reconciles: activeSession.ts says which race the service
// belongs to, the server says whether that race is still open, and the
// outcome is re-attach (recorder.start() over the live service — ready()
// is idempotent and pushes a FRESH auth token, draining any 401 backlog
// accumulated while JS was dead), or drain-and-stop (race ended/deleted,
// or descriptor with no surviving service).

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

import { getRace } from "../api/races";
import { useAuth } from "../auth/AuthContext";
import { registerPushToken } from "../notifications/pushTokens";
import {
  clearActiveSession,
  loadActiveSession,
} from "./activeSession";
import {
  isPluginTracking,
  requestBatteryOptimizationExemption,
  stopPlugin,
  syncNow,
} from "./backgroundGeolocation";
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
  /** Update the selected race IN PLACE with fresh server data (same id
      only). Unlike setSelectedRace this is NOT blocked while recording
      — the id doesn't change, so the recorder is unaffected. Added
      2026-07-07: RaceEditScreen saves weren't propagating here, so
      auto-start kept arming against the STALE start_at (the missed
      T-6 notification on the 07-07 test). */
  refreshSelectedRace: (race: Race) => void;
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

  // Same-id refresh after an edit. Functional update so we never race a
  // concurrent selection change; a mismatched id is a silent no-op.
  const refreshSelectedRace = useCallback((race: Race) => {
    setSelectedRaceState((prev) =>
      prev && prev.id === race.id ? race : prev,
    );
  }, []);

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

  // ── Phase 4 — keep native HTTP authed + register for server push ───
  //
  // Run only when there's a signed-in user. The token-refresh handlers
  // call auth.currentUser themselves, so the bare existence of a user
  // is sufficient to start them — they handle the moment-to-moment
  // refresh on AppState / onIdTokenChanged / interval ticks.
  //
  // registerPushToken (2026-07-05) piggybacks on the same gate: it
  // registers this device's FCM token with the backend so the
  // dead-recorder watchdog can reach the phone when the app itself is
  // dead. Best-effort + internally latched; see pushTokens.ts.
  //
  // No-op in JS-uploader mode: setAuthHeader is best-effort and the
  // plugin's http config block is absent when the flag is off, so the
  // header push is wasted work but not harmful. Phase 5 will remove
  // this branch once native uploader is the only code path.
  const { user } = useAuth();
  useEffect(() => {
    if (!user) return;
    void registerPushToken();
    return startTokenRefresh();
  }, [user]);

  // ── 2026-07-05 — relaunch reconciliation ────────────────────────────
  //
  // One shot per JS lifetime, gated on sign-in (getRace needs auth).
  // Decision table (session = activeSession.ts descriptor, running =
  // native service alive):
  //
  //   no session, not running   → nothing to do (normal launch)
  //   no session, running       → orphan service (descriptor write lost)
  //                               — drain queue, stop service
  //   session, race still open,
  //     native mode or running  → RE-ATTACH: select race, recorder.start()
  //   session, js mode, dead    → session died with the process; clear
  //                               descriptor (queue.ts backlog drains on
  //                               the next manual start — existing path)
  //   session, race ended/gone  → drain queue, stop service, clear
  //
  // The re-attach can't call recorder.start() in the same tick as
  // setSelectedRaceState — the hook reads raceId from the re-rendered
  // prop. pendingResumeRaceIdRef bridges: the effect below fires once
  // the selection has propagated.
  const reconciledRef = useRef(false);
  const pendingResumeRaceIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!user || reconciledRef.current) return;
    reconciledRef.current = true;

    void (async () => {
      const session = await loadActiveSession();
      const running = await isPluginTracking();

      if (!session) {
        if (running) {
          // Service alive with no descriptor — shouldn't happen, but a
          // running tracker nobody can stop is the worst outcome, so
          // drain and kill rather than leave it.
          // eslint-disable-next-line no-console
          console.warn("[RecorderContext] orphan tracking service — stopping");
          await syncNow();
          await stopPlugin();
        }
        return;
      }

      if (recorder.recording) return; // hot-reload etc. — already live

      if (session.mode !== "native" && !running) {
        // js-mode recording died with the process. Nothing to re-attach
        // (no surviving uploader); the persisted GPS queue for that race
        // drains on the next manual start via loadQueue(). Just clean up.
        await clearActiveSession();
        return;
      }

      let race: Race | null = null;
      try {
        race = await getRace(session.raceId);
      } catch {
        race = null; // deleted, or offline — treat as not-resumable
      }

      if (race && race.ended_at == null) {
        // Race still open — re-attach the UI to the (possibly still
        // running) session. start() is idempotent over a live service
        // and refreshes the native auth header, so a >1h-dead JS layer
        // whose uploads were 401ing drains its backlog right here.
        pendingResumeRaceIdRef.current = race.id;
        setSelectedRaceState(race);
      } else {
        // Race ended (sweep/manual) or gone. Push any queued fixes so
        // late telemetry lands (race_sweep may already have closed the
        // race — points still ingest), then stop for real.
        if (running) {
          await syncNow();
          await stopPlugin();
        }
        await clearActiveSession();
      }
    })();
  }, [user, recorder.recording]);

  // Second half of the re-attach: fires after the selection state has
  // propagated into useTrackRecorder's raceId prop.
  useEffect(() => {
    if (
      pendingResumeRaceIdRef.current != null &&
      selectedRace?.id === pendingResumeRaceIdRef.current &&
      !recorder.recording
    ) {
      pendingResumeRaceIdRef.current = null;
      // eslint-disable-next-line no-console
      console.log(
        "[RecorderContext] re-attaching to surviving recording for race",
        selectedRace.id,
      );
      void recorder.start();
    }
  }, [selectedRace, recorder]);

  const value = useMemo<RecorderCtx>(
    () => ({
      selectedRace,
      setSelectedRace,
      refreshSelectedRace,
      recorder,
      startRecording,
    }),
    [selectedRace, setSelectedRace, refreshSelectedRace, recorder, startRecording],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRecorder(): RecorderCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useRecorder must be used inside <RecorderProvider>");
  return v;
}
