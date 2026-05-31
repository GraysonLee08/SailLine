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
//        Stack                  (expo-router root navigator)
//
//   3. The auth gate. The (auth) and (app) route groups are mounted as
//      siblings; the redirect logic lives in their respective layouts.
//      The root Stack just owns the screen-options + tells the OS
//      what's behind the status bar.

import { useEffect } from "react";
import { StatusBar } from "expo-status-bar";
import { Stack } from "expo-router";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { GoogleSignin } from "@react-native-google-signin/google-signin";

import * as Notifications from "expo-notifications";

import { AuthProvider, useAuth } from "../src/auth/AuthContext";
import { RecorderProvider } from "../src/recorder/RecorderContext";
import { ThemeProvider, useTheme } from "../src/theme/ThemeProvider";
import {
  registerHandlers,
  requestNotificationPermission,
} from "../src/recorder/scheduledAutoStart";
import {
  ACTION_MARK_AS_PASSED,
  ACTION_SKIP_MARK,
  ACTION_STOP_RACE,
  registerRaceNotificationCategories,
} from "../src/notifications/raceCategories";
import { dismissMissedMarkNotification } from "../src/notifications/missedMark";
import { recordManualMarkPass } from "../src/api/races";
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

// Route actionable notification responses to the right API call. Runs
// at module scope so a watch-tap (Apple Watch, supported wearables) is
// honoured even if the user never opens the app first. We intentionally
// do NOT need the user to bring the app to foreground for Pass / Skip —
// those actions were defined with opensAppToForeground=false so the
// notification dismisses on tap and the API call runs in the
// background. Stop opens the app since recorder.stop() lives in the
// React tree.
//
// Auth: recordManualMarkPass calls apiFetch which reads the current
// Firebase ID token. Token must already be present (user signed in
// during the active recording session, which is when missed-mark
// notifications can fire). A cold-start tap without an active session
// is unlikely but would fail at the auth gate — the notification body
// invites the user to "tap to open" as a fallback.
Notifications.addNotificationResponseReceivedListener((response) => {
  const data = response.notification.request.content.data as
    | { kind?: string; raceId?: string; markIndex?: number }
    | undefined;
  if (!data || data.kind !== "missedMark") return;
  if (typeof data.raceId !== "string" || typeof data.markIndex !== "number") {
    return;
  }
  const { raceId, markIndex } = data;
  switch (response.actionIdentifier) {
    case ACTION_MARK_AS_PASSED:
    case ACTION_SKIP_MARK:
      // Both actions record a manual pass at the mark index. The
      // semantic difference (you actually rounded vs you're skipping
      // ahead) is captured by which button the user tapped — we could
      // distinguish via a separate API field later if it becomes
      // useful for the AI summary. For tonight: both result in the
      // detector advancing.
      void recordManualMarkPass(raceId, markIndex)
        .catch(() => {
          /* swallow — notification is best-effort */
        })
        .finally(() => {
          void dismissMissedMarkNotification(raceId);
        });
      break;
    case ACTION_STOP_RACE:
      // Stop opens the app; the Recording screen surfaces the Stop
      // button which the user taps. (We can't call recorder.stop()
      // here because the recorder lives inside React state.)
      void dismissMissedMarkNotification(raceId);
      break;
    default:
      // Default tap (no action button) just opens the app.
      void dismissMissedMarkNotification(raceId);
      break;
  }
});

export default function RootLayout() {
  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <ThemeProvider>
        <AuthProvider>
          <RecorderProvider>
            <ThemedStatusBar />
            <NotificationPermissionGate />
            <Stack
              screenOptions={{
                headerShown: false,
                animation: "fade",
                contentStyle: { backgroundColor: "transparent" },
              }}
            />
          </RecorderProvider>
        </AuthProvider>
      </ThemeProvider>
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
 * Request notification permission once after the user signs in. Idempotent
 * — the OS does not re-prompt if already answered. A denial silently
 * disables the T-6 reminder path; the T-5 BG-fetch fallback still works.
 *
 * Lives at this layer (not in a screen) because expo-router can mount
 * different screens depending on auth state, and the prompt should fire
 * exactly once per signed-in session, not per screen.
 */
function NotificationPermissionGate() {
  const { user } = useAuth();
  useEffect(() => {
    if (!user) return;
    void requestNotificationPermission();
  }, [user]);
  return null;
}
