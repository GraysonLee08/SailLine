// scheduledAutoStart.ts — OS-level auto-start fallbacks for the recorder.
//
// Pairs with useAutoStartRecorder.ts. The hook owns the in-foreground
// fast path (a plain setTimeout that fires precisely when the JS runtime
// is alive). THIS module owns the two OS-level fallbacks for when the JS
// runtime may be suspended or the user isn't watching the app:
//
//   1. T-6 min — a local notification via expo-notifications. Visible
//      reminder. Tapping it brings the app to the foreground; the open
//      side-effect causes useTrackRecorder.start() to fire via the
//      onFire callback the hook registered with us. Acts as the
//      "tap to start" path the user asked for.
//
//   2. T-5 min — a BackgroundFetch.scheduleTask. Calls onFire() silently
//      if the JS runtime is alive (foreground, background, or just-woken
//      by the OS for fetch). If the OS has fully killed the app, this
//      task fires headless — onFire will be null (React not mounted) and
//      we post a "Race starting now — open SailLine" notification as the
//      last-resort visible signal. We deliberately do NOT try to start
//      the recorder from headless: it would require bootstrapping the
//      Firebase auth token, the selected raceId, the polar/route cache,
//      etc. — too much surface area for an untested cold-start path.
//
// Idempotency: onFire() ultimately calls recorder.start(), which guards
// on recordingRef.current — so all three triggers (foreground timer,
// notification tap, BG fetch) can fire in any order and only the first
// one will actually start the recorder.
//
// Cancellation: scheduleAutoStart(raceId, startAtIso) replaces any prior
// schedule for the same raceId. cancelAutoStart(raceId) clears both the
// notification and the BG task. The hook calls cancel on cleanup so that
// editing start_at to a later time doesn't strand stale fallbacks.
//
// IMPORTANT: registerHandlers() MUST be called at module load (top of
// App.tsx), before React mounts. BackgroundFetch.configure() registers a
// headless task entry point that the OS may invoke without React in the
// picture. Calling it inside a useEffect would mean the headless wake
// has nothing to invoke.

import BackgroundFetch from "react-native-background-fetch";
import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

// ── Module-scoped state ────────────────────────────────────────────────

/**
 * The recorder's start() function, kept current by useAutoStartRecorder
 * via setOnFire(). Null when no race is loaded or the hook hasn't mounted
 * yet (e.g. headless wake before React boots).
 */
let onFire: (() => void | Promise<void>) | null = null;

/**
 * True once registerHandlers() has run. Guards against double-registration
 * during fast-refresh; BackgroundFetch.configure() is otherwise idempotent
 * but expo-notifications addNotificationResponseReceivedListener returns a
 * subscription each call.
 */
let handlersRegistered = false;
let notificationResponseSub: Notifications.Subscription | null = null;

// ── Task / notification ID conventions ─────────────────────────────────

const BG_TASK_PREFIX = "sailline.autoStart.";
const NOTIF_TAG_PREFIX = "sailline-autostart-";
const ANDROID_CHANNEL_ID = "race-start";

function bgTaskId(raceId: string): string {
  return `${BG_TASK_PREFIX}${raceId}`;
}

function notifTag(raceId: string): string {
  return `${NOTIF_TAG_PREFIX}${raceId}`;
}

// ── Public API ─────────────────────────────────────────────────────────

/**
 * Register OS-level handlers. Call once at module load (top of App.tsx),
 * BEFORE React renders. Idempotent.
 */
export async function registerHandlers(): Promise<void> {
  if (handlersRegistered) return;
  handlersRegistered = true;

  // Set the foreground notification behaviour: we want the OS to display
  // the heads-up banner even when the app is open. Without this, expo
  // suppresses the notification UI when the app is in the foreground —
  // which would hide the T-6 "tap to start" cue if the user is already
  // looking at the screen.
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowAlert: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
      shouldShowBanner: true,
      shouldShowList: true,
    }),
  });

  // Android requires an explicit channel to honour heads-up importance.
  // Channel created at HIGH so the T-6 notification surfaces over the
  // lock screen — the sailor may be wearing gloves and only glancing.
  if (Platform.OS === "android") {
    try {
      await Notifications.setNotificationChannelAsync(ANDROID_CHANNEL_ID, {
        name: "Race start reminders",
        importance: Notifications.AndroidImportance.HIGH,
        description:
          "Notifications 6 minutes before each race so you can start tracking.",
        sound: "default",
        vibrationPattern: [0, 250, 250, 250],
        lockscreenVisibility:
          Notifications.AndroidNotificationVisibility.PUBLIC,
      });
    } catch {
      // Channel creation is best-effort; if it fails the notification still
      // posts to the default channel.
    }
  }

  // When the user taps a notification, the OS launches the app; once
  // React mounts and the hook registers via setOnFire, this listener
  // calls onFire(). If the tap happens before the hook has registered
  // (cold start), the response is queued and replayed via
  // getLastNotificationResponseAsync inside the hook's mount effect.
  notificationResponseSub?.remove();
  notificationResponseSub = Notifications.addNotificationResponseReceivedListener(
    (response) => {
      const data = response.notification.request.content.data;
      if (data?.kind !== "autoStart") return;
      // Defer to next tick so React has a chance to mount and the hook
      // can register its onFire ref if it hasn't yet.
      setTimeout(() => {
        void onFire?.();
      }, 0);
    },
  );

  // Configure the BG fetch headless entry. The minimumFetchInterval is
  // required by BackgroundFetch.configure() but is not what triggers our
  // T-5 task — scheduleTask schedules its own one-shot at a specific
  // delay. We set 15min (the iOS minimum) to satisfy the API; the
  // recurring callback is a no-op for us.
  try {
    await BackgroundFetch.configure(
      {
        minimumFetchInterval: 15,
        stopOnTerminate: false,
        startOnBoot: true,
        enableHeadless: true,
        requiredNetworkType: BackgroundFetch.NETWORK_TYPE_NONE,
      },
      async (taskId) => {
        // Called by the OS when a scheduled task fires (foreground or
        // background; headless on Android uses the separate headless
        // task registration in index.ts).
        if (taskId.startsWith(BG_TASK_PREFIX)) {
          await handleAutoStartFire(taskId);
        }
        BackgroundFetch.finish(taskId);
      },
      async (taskId) => {
        // Timeout — OS is reclaiming the task. Just finish.
        BackgroundFetch.finish(taskId);
      },
    );
  } catch {
    // Configure can fail on iOS Simulator etc.; the schedule call below
    // will surface the real error.
  }
}

/**
 * Register the recorder's start() callback. Called by useAutoStartRecorder
 * on every render with the current `start` so the fallbacks always call
 * the latest closure (raceId, recorder ref, etc. stay current).
 *
 * Passing null unregisters — used when the hook unmounts or `enabled`
 * goes false.
 */
export function setOnFire(fn: (() => void | Promise<void>) | null): void {
  onFire = fn;
}

/**
 * Replay the last notification tap if it happened before the hook had a
 * chance to register onFire (cold start path). Called by the hook on
 * mount. Safe to call multiple times; tracks the last-handled id
 * locally so we don't fire twice for the same tap.
 */
let lastHandledResponseId: string | null = null;
export async function replayPendingTap(): Promise<void> {
  try {
    const response = await Notifications.getLastNotificationResponseAsync();
    if (!response) return;
    const id = response.notification.request.identifier;
    if (id === lastHandledResponseId) return;
    const data = response.notification.request.content.data;
    if (data?.kind !== "autoStart") return;
    lastHandledResponseId = id;
    void onFire?.();
  } catch {
    /* nothing actionable */
  }
}

/**
 * Schedule both fallbacks for a race. Replaces any prior schedule for
 * the same raceId. Silent no-op if the race is in the past or so far in
 * the future that scheduling would be wasteful (>24 h).
 *
 * Returns the (target) fire times so the caller can log/test.
 */
export type ScheduleResult = {
  notifFireAt: Date | null;
  bgFireAt: Date | null;
};

const T_MINUS_NOTIF_MS = 6 * 60 * 1000;
const T_MINUS_BG_MS = 5 * 60 * 1000;
const MAX_HORIZON_MS = 24 * 60 * 60 * 1000;

export async function scheduleAutoStart(
  raceId: string,
  startAtIso: string,
): Promise<ScheduleResult> {
  // Always clear before scheduling so the schedule is replaced, not
  // duplicated. Cheap: each cancel is a single OS call.
  await cancelAutoStart(raceId);

  const startAt = new Date(startAtIso).getTime();
  if (Number.isNaN(startAt)) return { notifFireAt: null, bgFireAt: null };

  const now = Date.now();
  const notifDelay = startAt - T_MINUS_NOTIF_MS - now;
  const bgDelay = startAt - T_MINUS_BG_MS - now;

  // Don't schedule if both fire times have already passed (race already
  // underway). The hook's foreground setTimeout path will handle
  // late-start cases on its own.
  if (notifDelay <= 0 && bgDelay <= 0) {
    return { notifFireAt: null, bgFireAt: null };
  }

  // Don't schedule >24h out either — Android's exact-alarm policy gets
  // unhappy with very long horizons, and the user may edit the race
  // many times before then. The hook will re-schedule when the user
  // comes within range.
  if (bgDelay > MAX_HORIZON_MS) {
    return { notifFireAt: null, bgFireAt: null };
  }

  let notifFireAt: Date | null = null;
  let bgFireAt: Date | null = null;

  // ── T-6 notification ─────────────────────────────────────────────────
  if (notifDelay > 0) {
    try {
      await Notifications.scheduleNotificationAsync({
        identifier: notifTag(raceId),
        content: {
          title: "Race starts in 6 min",
          body:
            "Tap to start tracking. SailLine will auto-start at T-5 if you don't.",
          data: { kind: "autoStart", raceId },
          ...(Platform.OS === "android"
            ? { channelId: ANDROID_CHANNEL_ID }
            : {}),
        },
        trigger: {
          type: Notifications.SchedulableTriggerInputTypes.DATE,
          date: new Date(now + notifDelay),
        } as Notifications.NotificationTriggerInput,
      });
      notifFireAt = new Date(now + notifDelay);
    } catch {
      // Notification scheduling can fail if permission is denied — that's
      // fine, the BG fetch task below is independent.
    }
  }

  // ── T-5 BackgroundFetch one-shot ─────────────────────────────────────
  if (bgDelay > 0) {
    try {
      await BackgroundFetch.scheduleTask({
        taskId: bgTaskId(raceId),
        delay: bgDelay,
        periodic: false,
        forceAlarmManager: true, // Android: use AlarmManager for precise timing
        stopOnTerminate: false,
        enableHeadless: true,
      });
      bgFireAt = new Date(now + bgDelay);
    } catch {
      // scheduleTask can fail on iOS Simulator. The notification path is
      // still active.
    }
  }

  return { notifFireAt, bgFireAt };
}

export async function cancelAutoStart(raceId: string): Promise<void> {
  try {
    await Notifications.cancelScheduledNotificationAsync(notifTag(raceId));
  } catch {
    /* no-op */
  }
  try {
    await BackgroundFetch.stop(bgTaskId(raceId));
  } catch {
    /* no-op */
  }
}

/**
 * Request notification permission. Idempotent — if already granted/denied
 * the OS does not re-prompt. Returns true if granted.
 *
 * Call once after sign-in (App.tsx AuthedShell). A denial silently
 * disables the notification path; the BG fetch fallback still works.
 */
export async function requestNotificationPermission(): Promise<boolean> {
  try {
    const { status: existing } = await Notifications.getPermissionsAsync();
    if (existing === "granted") return true;
    const { status } = await Notifications.requestPermissionsAsync({
      ios: { allowAlert: true, allowBadge: false, allowSound: true },
    });
    return status === "granted";
  } catch {
    return false;
  }
}

// ── Internal ───────────────────────────────────────────────────────────

async function handleAutoStartFire(taskId: string): Promise<void> {
  // If React + the hook have registered an onFire, call it. If not (full
  // cold wake from a killed app), post a "race starting" notification as
  // the last-resort signal — the user opens the app from the
  // notification and can hit Start manually.
  if (onFire) {
    try {
      await onFire();
    } catch {
      /* swallow; recorder.start() logs its own errors */
    }
    return;
  }

  // Headless / cold-start fallback notification.
  const raceId = taskId.slice(BG_TASK_PREFIX.length);
  try {
    await Notifications.scheduleNotificationAsync({
      content: {
        title: "Race starting now",
        body: "Open SailLine and tap Start to record this race.",
        data: { kind: "autoStartColdFallback", raceId },
        ...(Platform.OS === "android"
          ? { channelId: ANDROID_CHANNEL_ID }
          : {}),
      },
      trigger: null, // fire immediately
    });
  } catch {
    /* swallow */
  }
}
