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

import { StatusBar } from "expo-status-bar";
import { Stack } from "expo-router";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { GoogleSignin } from "@react-native-google-signin/google-signin";

import * as Notifications from "expo-notifications";

import { AuthProvider, useAuth } from "../src/auth/AuthContext";
import { RecorderProvider } from "../src/recorder/RecorderContext";
import { RoutingProvider } from "../src/routing/RoutingContext";
import { ThemeProvider, useTheme } from "../src/theme/ThemeProvider";
import { registerHandlers } from "../src/recorder/scheduledAutoStart";
import { PermissionWelcomeCard } from "../src/components/PermissionWelcomeCard";
import { registerRaceNotificationCategories } from "../src/notifications/raceCategories";
import { dismissMissedMarkNotification } from "../src/notifications/missedMark";
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
// is informational only (2026-06-08): there is no "mark as passed"
// action — the mark-rounding detector is the sole writer of mark_passes
// and a racer doesn't hand-confirm marks on the phone. The only action
// left is "Stop race", which opens the app (recorder.stop() lives in the
// React tree). Any tap — action or body — dismisses the notification.
Notifications.addNotificationResponseReceivedListener((response) => {
  const data = response.notification.request.content.data as
    | { kind?: string; raceId?: string; markIndex?: number }
    | undefined;
  if (!data || data.kind !== "missedMark") return;
  if (typeof data.raceId !== "string") return;
  const { raceId } = data;
  // Stop opens the app (opensAppToForeground on the action surfaces the
  // Recording screen's Stop button); every other tap is just an
  // acknowledgement. Both paths simply dismiss the notification.
  void dismissMissedMarkNotification(raceId);
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
