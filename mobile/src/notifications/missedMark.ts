// notifications/missedMark.ts — fire / dismiss the "missed mark" prompt.
//
// Companion to raceCategories.ts. The notifier hook in
// useMissedMarkNotifier.ts decides WHEN to fire; this module owns the
// posting + cancellation API (one notification per race, replaced if
// already shown so we never stack duplicates).
//
// The notification is informational (2026-06-08): it tells the sailor a
// mark may not have registered, but offers no "mark as passed" action —
// the mark-rounding detector is the sole writer of mark_passes. The
// body carries `data` so the response listener can dismiss it:
//   { kind: "missedMark", raceId, markIndex, markName }
//
// Response routing: the global addNotificationResponseReceivedListener
// (app/_layout.tsx) checks data.kind and dismisses. The only action
// button is ACTION_STOP_RACE, which opens the app (opensAppToForeground)
// so the Recording screen's Stop button is reachable; any other tap is
// just an acknowledgement.

import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import { CATEGORY_MISSED_MARK } from "./raceCategories";

const NOTIF_TAG_PREFIX = "sailline-missedmark-";

// Channel used by scheduledAutoStart — reuse so we only have to
// configure one Android channel. Importance HIGH gets heads-up.
const ANDROID_CHANNEL_ID = "race-start";

function tagFor(raceId: string): string {
  return `${NOTIF_TAG_PREFIX}${raceId}`;
}

export type MissedMarkPayload = {
  raceId: string;
  markIndex: number;
  markName: string;
};

/**
 * Post (or replace) the missed-mark notification for a race. Returns
 * the OS notification identifier so callers can correlate dismissal.
 *
 * Always-replace pattern: tagging by raceId means a second fire while
 * the first is still showing just updates the body. No duplicate
 * notifications, no notification spam if the boat oscillates near a
 * mark's threshold.
 */
export async function postMissedMarkNotification(
  payload: MissedMarkPayload,
): Promise<string | null> {
  try {
    // Cancel any prior to avoid OS-side stacking on iOS (where
    // replacing by identifier sometimes leaves a ghost). Cheap.
    await dismissMissedMarkNotification(payload.raceId);
    const id = await Notifications.scheduleNotificationAsync({
      identifier: tagFor(payload.raceId),
      content: {
        title: "Missed a mark?",
        body: `It looks like you passed ${payload.markName} but it didn't auto-register. It'll still be picked up from your track after the race.`,
        data: {
          kind: "missedMark",
          raceId: payload.raceId,
          markIndex: payload.markIndex,
          markName: payload.markName,
        },
        categoryIdentifier: CATEGORY_MISSED_MARK,
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

/** Cancel the missed-mark notification (e.g. once the mark is finally
 *  recorded, or the user dismisses without acting). */
export async function dismissMissedMarkNotification(
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
