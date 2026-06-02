// featureFlags.ts — local, AsyncStorage-backed feature flags for the
// recorder.
//
// Phase 4 of the durable upload pipeline rework
// (sailline-docs/2026-06-01_durable-upload-pipeline-plan.md).
//
// The Phase 4 native-uploader switch is the highest-risk change in
// the entire rework — if the Transistorsoft locationTemplate emits the
// wrong wire shape, every POST will 422 silently and we're worse off
// than where we started. To de-risk we ship the new path behind a
// runtime flag, OFF by default, with a toggle on the existing debug
// screen. You flip the flag on, start a recording, watch the debrief
// for `points_uploaded > 0` and `longest_success_gap_s` sane. If yes,
// next build makes it default-on. If no, flip back and we iterate
// without losing the ability to record a race.
//
// Flag scope is whole-app, not per-race. Changing the flag mid-session
// has no effect until the next `start()` — the recorder reads the
// flag once at start time and configures the watcher accordingly.
// The debug-screen toggle warns the user about this.
//
// Persistence: AsyncStorage. Survives app reloads but not uninstalls,
// which is the right behaviour for a developer-toggle: a fresh install
// (new EAS build) lands at the default for that build.

import AsyncStorage from "@react-native-async-storage/async-storage";

const STORAGE_PREFIX = "sailline.featureFlag.";

/** Flag keys are an enum so a typo at the call site is a compile error. */
export type FeatureFlag = "native_uploader";

/** Defaults baked into the build. Update here when promoting a flag
 *  from opt-in to opt-out. */
const DEFAULTS: Record<FeatureFlag, boolean> = {
  // Phase 4 ships OFF until the first on-water validation. After two
  // clean races we flip the default to true (and after the third we
  // delete the flag + JS uploader entirely — Phase 5 cleanup).
  native_uploader: false,
};

function storageKey(flag: FeatureFlag): string {
  return `${STORAGE_PREFIX}${flag}`;
}

/**
 * Read a flag's effective value. Returns the persisted override if
 * present, else the build default. Defensive: any storage error or
 * malformed value falls back to the default.
 *
 * Async — call once at recorder start() time and capture in a local
 * for the rest of the session.
 */
export async function getFlag(flag: FeatureFlag): Promise<boolean> {
  try {
    const raw = await AsyncStorage.getItem(storageKey(flag));
    if (raw === null) return DEFAULTS[flag];
    if (raw === "true") return true;
    if (raw === "false") return false;
    return DEFAULTS[flag];
  } catch {
    return DEFAULTS[flag];
  }
}

/**
 * Persist a flag override. Best-effort — storage errors are swallowed
 * since the caller can re-attempt and the worst outcome is "next read
 * sees the old value."
 */
export async function setFlag(flag: FeatureFlag, value: boolean): Promise<void> {
  try {
    await AsyncStorage.setItem(storageKey(flag), value ? "true" : "false");
  } catch {
    // ignore
  }
}

/**
 * Clear an override so the build default takes over on the next read.
 * Useful when promoting a flag default — bumping the default in
 * DEFAULTS will only take effect for users who haven't manually
 * toggled, so the promote commit can clear any user override at the
 * same time.
 */
export async function clearFlag(flag: FeatureFlag): Promise<void> {
  try {
    await AsyncStorage.removeItem(storageKey(flag));
  } catch {
    // ignore
  }
}

/** Snapshot the entire flag set for the debug screen. */
export async function getAllFlags(): Promise<Record<FeatureFlag, boolean>> {
  const entries = await Promise.all(
    (Object.keys(DEFAULTS) as FeatureFlag[]).map(
      async (k) => [k, await getFlag(k)] as const,
    ),
  );
  return Object.fromEntries(entries) as Record<FeatureFlag, boolean>;
}
