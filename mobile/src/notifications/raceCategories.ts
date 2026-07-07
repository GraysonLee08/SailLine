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

/** Fired when CPA detector likely missed a mark — see useMissedMarkNotifier.
 *  Actionable (2026-06-29): the notification now offers "Yes" (confirm
 *  missed — manually insert a mark pass) and "No" (dismiss and suppress
 *  further notifications for this mark). A third "Stop race" action
 *  remains for abandoning the race entirely. */
export const CATEGORY_MISSED_MARK = "sailline.missed-mark";

/** Action: sailor confirms they DID miss the mark — triggers a manual
 *  mark pass entry via POST /api/races/{id}/mark-pass so the detector
 *  can advance to the next mark. */
export const ACTION_MARK_MISSED = "mark-missed";

/** Action: sailor says they did NOT miss the mark (or already rounded
 *  it) — dismisses the notification and suppresses re-fires for this
 *  mark index for the rest of the race. */
export const ACTION_MARK_NOT_MISSED = "mark-not-missed";

/** Action: stop the current recording (treats race as DNF). */
export const ACTION_STOP_RACE = "stop-race";

/** Fired at start_at + 2 min ONLY if every auto-start tier failed to
 *  start the recorder — see notifications/raceEvents.ts::
 *  scheduleStartFailsafe. Cancelled the moment recording starts, so if
 *  the sailor ever sees this, tracking is genuinely NOT running. */
export const CATEGORY_START_FAILSAFE = "sailline.start-failsafe";

/** Action: start the recorder now. Dispatched through scheduledAutoStart's
 *  onFire bridge (the same path the T-6 tap and T-5 BG fetch use) — the
 *  response handler can't call the recorder hook directly. Opens the app
 *  so the recording screen surfaces once the recorder flips live. */
export const ACTION_START_RECORDING = "start-recording";

/** The T-6 "Race starts in 6 min" reminder — see scheduledAutoStart.ts.
 *  2026-07-06: gained an explicit "Start recording" action button. The
 *  body-tap path still works, but watches (Garmin especially) only
 *  surface Android notifications' ACTION BUTTONS, not body taps — on
 *  the wrist the tap-to-start cue was showing as Dismiss/Block App
 *  with no way to actually start. Same ACTION_START_RECORDING id as
 *  the failsafe so the response handler needs no new branch. */
export const CATEGORY_AUTO_START = "sailline.auto-start";

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
        identifier: ACTION_MARK_MISSED,
        buttonTitle: "Yes, missed it",
        options: { opensAppToForeground: false },
      },
      {
        identifier: ACTION_MARK_NOT_MISSED,
        buttonTitle: "No, rounded it",
        options: { opensAppToForeground: false },
      },
      {
        identifier: ACTION_STOP_RACE,
        buttonTitle: "Stop race",
        options: { opensAppToForeground: true, isDestructive: true },
      },
    ]);
    await Notifications.setNotificationCategoryAsync(
      CATEGORY_START_FAILSAFE,
      [
        {
          identifier: ACTION_START_RECORDING,
          buttonTitle: "Start recording",
          options: { opensAppToForeground: true },
        },
      ],
    );
    await Notifications.setNotificationCategoryAsync(CATEGORY_AUTO_START, [
      {
        identifier: ACTION_START_RECORDING,
        buttonTitle: "Start recording",
        options: { opensAppToForeground: true },
      },
    ]);
  } catch {
    // Category registration can fail on Expo Go (which we don't ship
    // anyway — see CLAUDE.md mobile section). Surface no error; the
    // missed-mark notifier degrades to a no-action notification, which
    // is still useful (the sailor sees the heads-up and can open the app).
  }
}
