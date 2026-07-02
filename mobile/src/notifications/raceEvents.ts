// notifications/raceEvents.ts — race lifecycle notifications.
//
// Companion to missedMark.ts / tactics.ts (same posting + always-replace
// pattern, same "race-start" Android channel so there's only one channel
// to configure). Covers the minimal set the user settled on in the
// 2026-07-02 interview:
//
//   * "Race completed"    — informational. Posted by the recording
//     screen's auto-stop effect when the server's v4 finish-gate
//     detector sets ended_at mid-recording. Body tap → /debrief/{id}
//     (handled in app/_layout.tsx's response listener).
//   * "Recording started" — informational. Posted by the auto-start
//     fire paths when the recorder starts while the app is NOT active
//     (backgrounded / screen-locked), so the sailor gets a wrist buzz
//     confirming tracking is live. Body tap → /recording.
//   * "Start recording" FAILSAFE — actionable. Scheduled at
//     start_at + 2 min when auto-start arms, and cancelled the moment
//     the recorder actually starts (any tier) or the race/arming
//     changes. If it ever fires, every auto-start tier failed and
//     recording is NOT running — the action button starts the recorder
//     via scheduledAutoStart's onFire bridge.
//
// The first two are informational only (no action buttons → no category
// needed; the 2026-05-30 notification standard requires buttons only
// for notifications that ask the user to DO something). The failsafe
// asks the user to do something, so it registers CATEGORY_START_FAILSAFE
// in raceCategories.ts.

import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import { CATEGORY_START_FAILSAFE } from "./raceCategories";

// Channel used by scheduledAutoStart — reuse so we only have to
// configure one Android channel. Importance HIGH gets heads-up.
const ANDROID_CHANNEL_ID = "race-start";

const COMPLETED_TAG_PREFIX = "sailline-racecomplete-";
const STARTED_TAG_PREFIX = "sailline-recstarted-";
const FAILSAFE_TAG_PREFIX = "sailline-startfailsafe-";

function completedTag(raceId: string): string {
  return `${COMPLETED_TAG_PREFIX}${raceId}`;
}

function startedTag(raceId: string): string {
  return `${STARTED_TAG_PREFIX}${raceId}`;
}

function failsafeTag(raceId: string): string {
  return `${FAILSAFE_TAG_PREFIX}${raceId}`;
}

// ── "Race completed" ───────────────────────────────────────────────────

export type RaceEventPayload = {
  raceId: string;
  raceName: string;
};

/**
 * Post (or replace) the race-completed notification. Fired by the
 * recording screen when the server ends the race (finish gate crossed)
 * and the phone reacts by stopping the recorder. Informational — tap
 * opens the app and deep-links to the debrief.
 */
export async function postRaceCompletedNotification(
  payload: RaceEventPayload,
): Promise<string | null> {
  try {
    await dismissRaceCompletedNotification(payload.raceId);
    const id = await Notifications.scheduleNotificationAsync({
      identifier: completedTag(payload.raceId),
      content: {
        title: "Race completed",
        body: `${payload.raceName}: Recording stopped — your debrief is ready.`,
        data: { kind: "raceCompleted", raceId: payload.raceId },
        ...(Platform.OS === "android"
          ? { channelId: ANDROID_CHANNEL_ID }
          : {}),
      },
      trigger: null, // immediate
    });
    return id;
  } catch {
    return null;
  }
}

/** Cancel/dismiss the race-completed notification. */
export async function dismissRaceCompletedNotification(
  raceId: string,
): Promise<void> {
  try {
    await Notifications.cancelScheduledNotificationAsync(completedTag(raceId));
  } catch {
    /* no-op */
  }
  try {
    await Notifications.dismissNotificationAsync(completedTag(raceId));
  } catch {
    /* no-op */
  }
}

// ── "Recording started" ────────────────────────────────────────────────

/**
 * Post (or replace) the recording-started notification. Called by the
 * auto-start fire paths ONLY when the app is not active — an in-app
 * start already shows the green "Auto-start activated" banner on the
 * recording screen, so a notification on top would be noise.
 */
export async function postRecordingStartedNotification(
  payload: RaceEventPayload,
): Promise<string | null> {
  try {
    await dismissRecordingStartedNotification(payload.raceId);
    const id = await Notifications.scheduleNotificationAsync({
      identifier: startedTag(payload.raceId),
      content: {
        title: "Recording started",
        body: `${payload.raceName}: Auto-start fired — tracking is live.`,
        data: { kind: "recordingStarted", raceId: payload.raceId },
        ...(Platform.OS === "android"
          ? { channelId: ANDROID_CHANNEL_ID }
          : {}),
      },
      trigger: null, // immediate
    });
    return id;
  } catch {
    return null;
  }
}

/** Cancel/dismiss the recording-started notification. */
export async function dismissRecordingStartedNotification(
  raceId: string,
): Promise<void> {
  try {
    await Notifications.cancelScheduledNotificationAsync(startedTag(raceId));
  } catch {
    /* no-op */
  }
  try {
    await Notifications.dismissNotificationAsync(startedTag(raceId));
  } catch {
    /* no-op */
  }
}

// ── "Start recording" failsafe ─────────────────────────────────────────

// Fire 2 minutes after the gun. All three auto-start tiers (foreground
// setTimeout at T-5, T-6 notification tap, T-5 BG fetch) should have
// started the recorder long before then — so if this ever surfaces,
// recording genuinely never started and the sailor needs to act.
const FAILSAFE_OFFSET_MS = 2 * 60 * 1000;

// Mirror scheduledAutoStart's horizon guard: Android's exact-alarm
// policy gets unhappy with very long horizons, and the user may edit
// the race many times before then. The hook re-schedules when the
// user comes within range.
const MAX_HORIZON_MS = 24 * 60 * 60 * 1000;

/**
 * Schedule the failsafe for a race. Replaces any prior schedule for the
 * same raceId (cancel-then-schedule, like scheduledAutoStart). Silent
 * no-op when the fire time is already past (auto-start's late-arm path
 * handles races the user opens after the gun — they're in-app) or the
 * race is >24 h out.
 *
 * Lifecycle contract (owned by useAutoStartRecorder — same conditions
 * as the T-6/T-5 fallbacks): armed when auto-start arms ahead of the
 * start, cancelled via cancelStartFailsafe() the moment the recorder
 * starts, or when the race/arming changes.
 */
export async function scheduleStartFailsafe(
  raceId: string,
  startAtIso: string,
): Promise<Date | null> {
  await cancelStartFailsafe(raceId);

  const startAt = new Date(startAtIso).getTime();
  if (Number.isNaN(startAt)) return null;

  const fireAt = startAt + FAILSAFE_OFFSET_MS;
  const delay = fireAt - Date.now();
  if (delay <= 0 || delay > MAX_HORIZON_MS) return null;

  try {
    await Notifications.scheduleNotificationAsync({
      identifier: failsafeTag(raceId),
      content: {
        title: "Race started — recording is NOT running",
        body:
          "Auto-start never fired. Tap Start recording to begin tracking.",
        data: { kind: "startFailsafe", raceId },
        categoryIdentifier: CATEGORY_START_FAILSAFE,
        ...(Platform.OS === "android"
          ? { channelId: ANDROID_CHANNEL_ID }
          : {}),
      },
      trigger: {
        type: Notifications.SchedulableTriggerInputTypes.DATE,
        date: new Date(fireAt),
      } as Notifications.NotificationTriggerInput,
    });
    return new Date(fireAt);
  } catch {
    // Scheduling can fail if permission is denied — nothing to fall
    // back to; the in-app UI still shows recording state.
    return null;
  }
}

/**
 * Cancel the failsafe (recorder started, race changed, arming cleared)
 * AND dismiss it if it already fired — the recorder starting after the
 * failsafe surfaced (user tapped the action button) should clear the
 * scary "NOT running" banner from the shade.
 */
export async function cancelStartFailsafe(raceId: string): Promise<void> {
  try {
    await Notifications.cancelScheduledNotificationAsync(failsafeTag(raceId));
  } catch {
    /* no-op */
  }
  try {
    await Notifications.dismissNotificationAsync(failsafeTag(raceId));
  } catch {
    /* no-op */
  }
}
