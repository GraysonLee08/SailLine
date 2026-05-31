// notifications/raceCategories.ts — actionable notification categories
// for race events. Each category defines the buttons that appear on the
// notification (Garmin Fenix, Apple Watch, lock screen, banner).
//
// Why categories: expo-notifications' actionable buttons require a
// pre-registered "category" (Apple's term) that the OS associates with
// the notification via `categoryIdentifier`. The buttons + their
// identifiers are pinned at registration time; the notification just
// references the category by name.
//
// Watch behaviour:
//   * Apple Watch — relays the notification AND its actions; tapping
//     an action on the watch sends the response back to the phone where
//     `addNotificationResponseReceivedListener` fires.
//   * Garmin Fenix 8 (and similar) — relays the notification text via
//     Garmin Connect Mobile's "smart notifications" channel. Action
//     button visibility on Garmin varies by firmware: text + reply
//     work on most models, custom action buttons surface inconsistently
//     and Garmin does not document a stable API for them. We define
//     the actions anyway so iOS/Apple-Watch/Android-wear users get
//     full control; on Garmin the user can still see the notification
//     and tap it on the phone.
//
// Adding a new category: append to RACE_NOTIFICATION_CATEGORIES below
// and add the matching action handler in App.tsx (or a future
// `notifications/router.ts`). Pattern is: every actionable notification
// MUST register a category — no actionable notification posts without
// buttons, that defeats the watch-actionability the spec calls for.
//
// 2026-05-30 spec: see sailline-docs/2026-05-30_session.md "Notification
// action button standard" — any notification that asks the user to do
// something must offer buttons reachable from the wrist.

import * as Notifications from "expo-notifications";

// ── Category + action identifiers (stable strings — do not rename) ─

/** Fired when CPA detector likely missed a mark — see useMissedMarkNotifier. */
export const CATEGORY_MISSED_MARK = "sailline.missed-mark";

/** Action: mark the next-expected mark as passed (calls /mark-passes). */
export const ACTION_MARK_AS_PASSED = "mark-as-passed";

/** Action: skip the mark (advance the detector by writing a manual pass
 *  with a synthetic timestamp + lat/lon = mark's nominal position). */
export const ACTION_SKIP_MARK = "skip-mark";

/** Action: stop the current recording (treats race as DNF). */
export const ACTION_STOP_RACE = "stop-race";

// ── Registration ──────────────────────────────────────────────────────

/**
 * Register all race-related notification categories. Idempotent —
 * expo-notifications.setNotificationCategoryAsync replaces an existing
 * category with the same identifier. Call once from App.tsx alongside
 * scheduledAutoStart.registerHandlers().
 */
export async function registerRaceNotificationCategories(): Promise<void> {
  try {
    await Notifications.setNotificationCategoryAsync(CATEGORY_MISSED_MARK, [
      {
        identifier: ACTION_MARK_AS_PASSED,
        buttonTitle: "Mark as passed",
        options: { opensAppToForeground: false },
      },
      {
        identifier: ACTION_SKIP_MARK,
        buttonTitle: "Skip",
        options: { opensAppToForeground: false },
      },
      {
        identifier: ACTION_STOP_RACE,
        buttonTitle: "Stop race",
        options: { opensAppToForeground: true, isDestructive: true },
      },
    ]);
  } catch {
    // Category registration can fail on Expo Go (which we don't ship
    // anyway — see CLAUDE.md mobile section). Surface no error; the
    // missed-mark notifier degrades to a no-action notification, which
    // is still useful (the user opens the app and taps Pass manually).
  }
}
