// activeSession.ts — AsyncStorage descriptor of the in-flight recording.
//
// Written by useTrackRecorder.start(), cleared by stop(). Exists for
// exactly one reader: the relaunch reconciler in RecorderContext. With
// stopOnTerminate:false + startOnBoot:true (2026-07-05), Transistorsoft's
// native service can outlive the JS process — an OS kill or a reboot
// leaves the foreground service capturing (and, in native-uploader mode,
// POSTing) with no React tree above it. On the next app launch this
// descriptor is how JS re-discovers WHICH race the still-running (or
// just-revived) service belongs to, so the recorder UI can re-attach
// instead of showing a dead Start button over a live recording.
//
// Not a queue, not a cache — a single pointer. The GPS data itself is
// owned by Transistorsoft's SQLite store (native mode) or
// queue.ts/AsyncStorage (js mode), both of which already survive
// restarts on their own.

import AsyncStorage from "@react-native-async-storage/async-storage";

const KEY = "sailline.activeSession";

export type ActiveSession = {
  raceId: string;
  /** Recorder mode snapshotted at start(). Reconciliation only
   *  re-attaches "native" sessions — a killed js-mode session has no
   *  surviving uploader, so it is reported as interrupted instead. */
  mode: "js" | "native";
  /** ISO timestamp of recorder.start(), for display/debugging. */
  startedAt: string;
};

/** Persist the descriptor. Best-effort; a write failure only costs the
 *  relaunch re-attach (recording itself is unaffected). */
export async function saveActiveSession(s: ActiveSession): Promise<void> {
  try {
    await AsyncStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    // ignore
  }
}

/** Load the descriptor, or null when absent/unparseable. */
export async function loadActiveSession(): Promise<ActiveSession | null> {
  try {
    const raw = await AsyncStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ActiveSession;
    if (typeof parsed?.raceId !== "string" || !parsed.raceId) return null;
    return parsed;
  } catch {
    return null;
  }
}

/** Remove the descriptor (recorder stopped cleanly, or reconciliation
 *  decided the session is dead). */
export async function clearActiveSession(): Promise<void> {
  try {
    await AsyncStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}
