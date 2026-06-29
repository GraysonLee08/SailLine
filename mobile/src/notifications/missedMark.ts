// notifications/missedMark.ts — fire / dismiss the "missed mark" prompt.
//
// Companion to raceCategories.ts. The notifier hook in
// useMissedMarkNotifier.ts decides WHEN to fire; this module owns the
// posting + cancellation API (one notification per race, replaced if
// already shown so we never stack duplicates).
//
// Actionable (2026-06-29): the notification now offers "Yes, missed it"
// (manually insert a mark pass so the detector advances) and "No, rounded
// it" (suppress further notifications for this mark). The response handler
// in app/_layout.tsx dispatches these actions.
//
// Suppression: when the sailor taps "No, rounded it", the mark index is
// added to a module-scoped Set so useMissedMarkNotifier can skip re-firing
// for that mark for the rest of the race. Cleared on race id change.

import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import { CATEGORY_MISSED_MARK } from "./raceCategories";

const NOTIF_TAG_PREFIX = "sailline-missedmark-";

// Channel used by scheduledAutoStart — reuse so we only have to
// configure one Android channel. Importance HIGH gets heads-up.
const ANDROID_CHANNEL_ID = "race-start";

// ── Mark-level suppression ──────────────────────────────────────────────
//
// When the sailor taps "No, rounded it" we suppress further missed-mark
// notifications for that (raceId, markIndex) pair for the rest of the
// race. Without this the notifier would re-fire every 3 min (its cooldown)
// for a mark the sailor has already acknowledged.
const suppressedMarks = new Map<string, Set<number>>();

/** Record that the sailor dismissed a missed-mark prompt for a specific
 *  mark — suppresses re-firing until the race changes or the app restarts. */
export function suppressMark(raceId: string, markIndex: number): void {
  let set = suppressedMarks.get(raceId);
  if (!set) {
    set = new Set();
    suppressedMarks.set(raceId, set);
  }
  set.add(markIndex);
}

/** Check whether a mark has been suppressed for this race. */
export function isMarkSuppressed(raceId: string, markIndex: number): boolean {
  const set = suppressedMarks.get(raceId);
  return set ? set.has(markIndex) : false;
}

/** Clear all suppressions for a race (called on race change or stop). */
export function clearSuppressions(raceId: string): void {
  suppressedMarks.delete(raceId);
}

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
        body: `You passed ${payload.markName} but it didn't auto-register. Tap "Yes" to record it manually, or "No" if you already rounded it.`,
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
