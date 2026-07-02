// app/_layout.tsx — root layout. Loaded on every cold start.
//
// Three responsibilities, layered top to bottom:
//
//   1. Module-level side effects (Google Sign-In config + OS-level
//      auto-start handler registration) MUST run before React renders
//      the first frame so a cold-start notification tap or headless
//      BackgroundFetch wake finds its handlers. Registering inside a
//      useEffect would mean the handler is null at the moment the OS
//      invokes it. These live at module scope on purpose.
//
//   2. Providers — order matters:
//        ThemeProvider          (no deps)
//        GestureHandlerRootView (required by bottom-sheet + reanimated;
//                                must wrap navigators)
//        AuthProvider           (uses Firebase auth — no React deps)
//        RecorderProvider       (depends on Firebase auth indirectly via
//                                the recorder hook's apiFetch)
//        RoutingProvider        (reads selectedRace from RecorderProvider;
//                                hoists the computed route + better-route SSE
//                                so both Home and Recording share one
//                                instance)
//        Stack                  (expo-router root navigator)
//
//   3. The auth gate. The (auth) and (app) route groups are mounted as
//      siblings; the redirect logic lives in their respective layouts.
//      The root Stack just owns the screen-options + tells the OS
//      what's behind the status bar.

import { useEffect, useRef } from "react";
import { StatusBar } from "expo-status-bar";
import { Stack, router, usePathname } from "expo-router";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { GoogleSignin } from "@react-native-google-signin/google-signin";

import * as Notifications from "expo-notifications";

import { AuthProvider, useAuth } from "../src/auth/AuthContext";
import { RecorderProvider, useRecorder } from "../src/recorder/RecorderContext";
import { RoutingProvider } from "../src/routing/RoutingContext";
import { ThemeProvider, useTheme } from "../src/theme/ThemeProvider";
import { registerHandlers } from "../src/recorder/scheduledAutoStart";
import { PermissionWelcomeCard } from "../src/components/PermissionWelcomeCard";
import { registerRaceNotificationCategories } from "../src/notifications/raceCategories";
import {
  ACTION_MARK_MISSED,
  ACTION_MARK_NOT_MISSED,
  ACTION_STOP_RACE,
} from "../src/notifications/raceCategories";
import {
  dismissMissedMarkNotification,
  suppressMark,
} from "../src/notifications/missedMark";
import { manualMarkPass } from "../src/api/races";
import { GOOGLE_WEB_CLIENT_ID } from "../src/googleAuthConfig";

// ── Module-load side effects ──────────────────────────────────────────
//
// Run exactly once per JS process. These do NOT depend on React; calling
// them again from a useEffect would be a bug (would double-configure
// Google Sign-In + double-register the BG-fetch task).
GoogleSignin.configure({
  webClientId: GOOGLE_WEB_CLIENT_ID,
  offlineAccess: false,
});
void registerHandlers();
void registerRaceNotificationCategories();

// Route actionable notification responses. The missed-mark notification
// (2026-06-29) is now actionable with three buttons:
//   * "Yes, missed it"  → POST /api/races/{id}/mark-pass to manually
//     insert a mark pass so the detector advances to the next mark.
//   * "No, rounded it"  → suppress further notifications for this mark
//     for the rest of the race.
//   * "Stop race"       → opens the app (opensAppToForeground) so the
//     Recording screen's Stop button is reachable.
// Any body tap (no action) is just an acknowledgement — dismiss only.
//
// Race-event notifications (2026-07-02, notifications/raceEvents.ts)
// are informational — a body tap deep-links to the relevant screen:
//   * "Race completed"    → /debrief/{raceId}
//   * "Recording started" → /recording
// The start-failsafe's "Start recording" action button is NOT handled
// here: starting the recorder needs the onFire bridge that
// scheduledAutoStart owns, so its own response listener dispatches it
// (same path as a T-6 notification tap).
Notifications.addNotificationResponseReceivedListener((response) => {
  const data = response.notification.request.content.data as
    | { kind?: string; raceId?: string; markIndex?: number; markName?: string }
    | undefined;
  if (!data || typeof data.raceId !== "string") return;
  const { raceId } = data;

  if (data.kind === "raceCompleted") {
    // Defer a tick (same trick as scheduledAutoStart's listener) so a
    // launch-from-notification has a mounted navigator to act on.
    setTimeout(() => router.replace(`/debrief/${raceId}`), 0);
    return;
  }
  if (data.kind === "recordingStarted") {
    setTimeout(() => router.replace("/recording"), 0);
    return;
  }

  if (data.kind !== "missedMark") return;
  const markIndex = typeof data.markIndex === "number" ? data.markIndex : -1;

  const actionId = response.actionIdentifier;

  if (actionId === ACTION_MARK_MISSED) {
    // Sailor confirmed they missed the mark — manually insert a pass
    // so the detector can advance. Best-effort: errors log but don't
    // crash the notification handler. The notification is dismissed
    // regardless (a failure here just means the sailor will see the
    // next missed-mark prompt on the next cooldown cycle).
    if (markIndex >= 0) {
      void manualMarkPass(raceId, markIndex).catch((e) => {
        // eslint-disable-next-line no-console
        console.error("[missedMark] manual pass failed:", e);
      });
    }
    void dismissMissedMarkNotification(raceId);
  } else if (actionId === ACTION_MARK_NOT_MISSED) {
    // Sailor says they rounded it — suppress further prompts for this
    // mark for the rest of the race.
    if (markIndex >= 0) {
      suppressMark(raceId, markIndex);
    }
    void dismissMissedMarkNotification(raceId);
  } else if (actionId === ACTION_STOP_RACE) {
    // Stop opens the app — the Recording screen's Stop button is
    // reachable once the app surfaces. Just dismiss the notification.
    void dismissMissedMarkNotification(raceId);
  } else {
    // Body tap or unknown action — dismiss only.
    void dismissMissedMarkNotification(raceId);
  }
});

export default function RootLayout() {
  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      {/* SafeAreaProvider feeds insets to react-native-safe-area-context's
          SafeAreaView so children clear the Android status bar + clock
          + battery icons. The base react-native SafeAreaView is iOS-only
          (notch + home indicator); on Android it does nothing, which is
          why the hamburger chip and back chip were rendering behind the
          system icons (see 2026-06-04 user report, item 3). */}
      <SafeAreaProvider>
        <ThemeProvider>
          <AuthProvider>
            <RecorderProvider>
              <RoutingProvider>
                <ThemedStatusBar />
                <WelcomeGate />
                <RecordingGate />
                <Stack
                  screenOptions={{
                    headerShown: false,
                    animation: "fade",
                    contentStyle: { backgroundColor: "transparent" },
                  }}
                />
              </RoutingProvider>
            </RecorderProvider>
          </AuthProvider>
        </ThemeProvider>
      </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}

/**
 * Drive the OS status bar style from the resolved theme. expo-status-bar
 * accepts "light" | "dark" | "auto"; we map directly off the theme so a
 * user-driven theme change updates the bar without a reload.
 */
function ThemedStatusBar() {
  const { mode } = useTheme();
  return <StatusBar style={mode === "dark" ? "light" : "dark"} translucent />;
}

/**
 * Show the permission welcome card once per install after the user
 * signs in. The card runs notifications + location (Always) + activity
 * prompts in sequence and surfaces per-row results so the user can see
 * what they granted. Replaces the old NotificationPermissionGate, which
 * only asked for notifications and left location to be prompted lazily
 * by BackgroundGeolocation.ready() — a path the 2026-06-04 user report
 * showed was not discoverable.
 *
 * Lives at this layer (not inside a screen) for the same reason as
 * before: expo-router can mount different screens depending on auth
 * state and the welcome card should anchor to "user just signed in,"
 * not "user happens to be on screen X."
 */
function WelcomeGate() {
  const { user } = useAuth();
  return <PermissionWelcomeCard visible={!!user} />;
}

/**
 * Global recording → /recording navigation (2026-07-02).
 *
 * The Beer Can 7.1.2026 race exposed the gap: the only "recording
 * started → show the recording screen" effect lived on the HOME screen
 * (app/(app)/index.tsx), so when auto-start fired while the app was
 * backgrounded or on any other screen, recording began invisibly and
 * the user had to press Start manually just to see the tracking view.
 *
 * This gate lives at the root — inside RecorderProvider, above the
 * navigator — so it sees the recorder from every screen. It navigates
 * in exactly two situations:
 *
 *   1. The recorder TRANSITIONS to recording (auto-start or a manual
 *      start from any screen).
 *   2. Cold start with a recording already active (crash / process-
 *      death recovery) — the mount-time check below.
 *
 * It deliberately does NOT pin the user to /recording: once the
 * navigation has happened for a given recording session, the user is
 * free to visit Settings etc. mid-race without being yanked back.
 */
function RecordingGate() {
  const { recorder, selectedRace } = useRecorder();
  const pathname = usePathname();
  // Tracks whether we've already navigated for the CURRENT recording
  // session. Reset when recording stops so the next race navigates again.
  const handledRef = useRef(false);

  useEffect(() => {
    if (!recorder.recording) {
      handledRef.current = false;
      return;
    }
    if (handledRef.current) return;
    handledRef.current = true;
    // Without a selected race /recording bounces straight back to "/"
    // (its own guard) — skip the churn.
    if (!selectedRace) return;
    if (pathname !== "/recording") {
      router.replace("/recording");
    }
  }, [recorder.recording, selectedRace, pathname]);

  return null;
}
