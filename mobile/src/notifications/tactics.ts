// notifications/tactics.ts — local notification for AI tactician calls.
//
// Companion to missedMark.ts (same posting + always-replace pattern).
// Fired by the recording screen when a tactics call arrives while the
// app is backgrounded / screen-locked — the cockpit case where a
// banner the user can't see is useless but a watch buzz is exactly
// right.
//
// Informational only (no action buttons → no category needed; the
// 2026-05-30 notification standard requires buttons only for
// notifications that ask the user to DO something in the app — a
// tactics call is acted on with sail trim, not a tap). data.kind lets
// the global response listener treat a tap as a plain "open app".

import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

const NOTIF_TAG_PREFIX = "sailline-tactics-";

// Reuse the existing Android channel (race-start, importance HIGH) so
// the call gets heads-up + watch relay without another channel to
// configure.
const ANDROID_CHANNEL_ID = "race-start";

function tagFor(raceId: string): string {
  return `${NOTIF_TAG_PREFIX}${raceId}`;
}

export type TacticsNotificationPayload = {
  raceId: string;
  message: string;
  callType: string;
};

/**
 * Post (or replace) the tactics-call notification for a race.
 * Always-replace by raceId tag: a newer call supersedes the previous
 * one rather than stacking — mid-race the latest call is the only one
 * that matters.
 */
export async function postTacticsNotification(
  payload: TacticsNotificationPayload,
): Promise<string | null> {
  try {
    await dismissTacticsNotification(payload.raceId);
    const id = await Notifications.scheduleNotificationAsync({
      identifier: tagFor(payload.raceId),
      content: {
        title: "Tactician",
        body: payload.message,
        data: {
          kind: "tactics",
          raceId: payload.raceId,
          callType: payload.callType,
        },
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

/** Cancel the tactics notification (race stopped, user dismissed). */
export async function dismissTacticsNotification(
  raceId: string,
): Promise<void> {
  try {
    await Notifications.cancelScheduledNotificationAsync(tagFor(raceId));
  } catch {
    /* no-op */
  }
  try {
    await Notifications.dismissNotificationAsync(tagFor(raceId));
  } catch {
    /* no-op */
  }
}
