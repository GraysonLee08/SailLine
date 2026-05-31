// notifications/missedMark.ts — fire / dismiss the "missed mark" prompt.
//
// Companion to raceCategories.ts. The notifier hook in
// useMissedMarkNotifier.ts decides WHEN to fire; this module owns the
// posting + cancellation API (one notification per race, replaced if
// already shown so we never stack duplicates).
//
// The notification body carries `data` the response listener uses to
// route actions to the right race + mark:
//   { kind: "missedMark", raceId, markIndex, markName }
//
// Response routing: App.tsx (or wherever the global
// addNotificationResponseReceivedListener lives) checks data.kind, then
// dispatches one of:
//   ACTION_MARK_AS_PASSED  → POST /api/races/{raceId}/mark-passes
//                            with markIndex (backfills if needed).
//   ACTION_SKIP_MARK       → same POST but we treat it as "advance
//                            past this mark without claiming you
//                            rounded it" — server still records a
//                            manual pass at the mark's nominal point
//                            because the detector needs to advance.
//                            UI distinguishes via the "manual" label.
//   ACTION_STOP_RACE       → opens the app (we set
//                            opensAppToForeground), Recording screen
//                            handles the rest.

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
        body: `It looks like you passed ${payload.markName} but it didn't register. Confirm from the watch or tap to open SailLine.`,
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
