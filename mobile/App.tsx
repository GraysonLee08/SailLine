// App.tsx — root component.
//
// Three responsibilities, layered top to bottom:
//   1. Configure Google Sign-In + register OS-level auto-start handlers
//      (module load — these MUST run before React mounts so headless
//      BackgroundFetch wakes and notification taps can find their
//      handlers).
//   2. AuthGate: wait for Firebase auth state, render sign-in or the
//      authed shell.
//   3. AuthedShell: own the screen state machine (RacePicker → Recorder)
//      and the long-lived useTrackRecorder hook whose lifetime is the
//      signed-in session — not a particular screen. Also requests
//      notification permission once on first sign-in.
//
// Why the recorder lives in AuthedShell and not RecorderScreen: if the
// user navigates back to the picker mid-recording (UI prevents this,
// but defensive), unmounting RecorderScreen would tear down the GPS
// watcher. Hoisting the hook keeps the recorder alive across screen
// changes; RecorderScreen is purely presentational.
//
// Sign-in flow is unchanged from the Phase 1 harness — same native
// Google sign-in + Firebase credential exchange that's been proven on
// device.

import { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Button,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { StatusBar } from "expo-status-bar";
import {
  GoogleSignin,
  isErrorWithCode,
  isSuccessResponse,
  statusCodes,
} from "@react-native-google-signin/google-signin";
import {
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithCredential,
  signOut,
} from "firebase/auth";
import type { User } from "firebase/auth";

import { auth } from "./src/firebase";
import { GOOGLE_WEB_CLIENT_ID } from "./src/googleAuthConfig";
import { requestBatteryOptimizationExemption } from "./src/recorder/backgroundGeolocation";
import {
  registerHandlers,
  requestNotificationPermission,
} from "./src/recorder/scheduledAutoStart";
import { useTrackRecorder } from "./src/recorder/useTrackRecorder";
import RacePickerScreen from "./src/screens/RacePickerScreen";
import RecorderScreen from "./src/screens/RecorderScreen";
import type { Race } from "./src/types";

GoogleSignin.configure({
  webClientId: GOOGLE_WEB_CLIENT_ID,
  offlineAccess: false,
});

// Register notification + BackgroundFetch handlers at module load. This
// is fire-and-forget — registerHandlers is idempotent and any errors are
// swallowed inside (they'd be invisible on Simulator anyway). MUST run
// before React mounts so a headless BG-fetch wake or a cold-start
// notification tap can find its handlers.
void registerHandlers();

// ── Auth gate ──────────────────────────────────────────────────────────
export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [signingIn, setSigningIn] = useState(false);

  useEffect(() => {
    const unsub = onAuthStateChanged(auth, (u) => {
      setUser(u);
      setAuthReady(true);
    });
    return unsub;
  }, []);

  const handleSignIn = async () => {
    setSigningIn(true);
    setAuthError(null);
    try {
      await GoogleSignin.hasPlayServices({
        showPlayServicesUpdateDialog: true,
      });

      const response = await GoogleSignin.signIn();
      if (!isSuccessResponse(response)) {
        setSigningIn(false);
        return;
      }

      const idToken = response.data.idToken;
      if (!idToken) {
        setAuthError("Google sign-in returned no idToken.");
        setSigningIn(false);
        return;
      }

      const credential = GoogleAuthProvider.credential(idToken);
      await signInWithCredential(auth, credential);
    } catch (e) {
      if (isErrorWithCode(e)) {
        switch (e.code) {
          case statusCodes.SIGN_IN_CANCELLED:
            break;
          case statusCodes.IN_PROGRESS:
            setAuthError("Sign-in already in progress.");
            break;
          case statusCodes.PLAY_SERVICES_NOT_AVAILABLE:
            setAuthError("Google Play Services not available on this device.");
            break;
          default:
            setAuthError(`Sign-in failed (${e.code}): ${e.message}`);
        }
      } else {
        setAuthError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setSigningIn(false);
    }
  };

  const handleSignOut = async () => {
    try {
      await GoogleSignin.signOut();
    } catch {
      /* best effort */
    }
    await signOut(auth);
  };

  if (!authReady) {
    return (
      <View style={[styles.container, styles.centered]}>
        <ActivityIndicator color="#8fb4c7" />
        <StatusBar style="light" />
      </View>
    );
  }

  if (!user) {
    return (
      <ScrollView contentContainerStyle={styles.container}>
        <Text style={styles.title}>SailLine</Text>
        <Text style={styles.subtitle}>
          Sign in with Google to record telemetry
        </Text>

        {authError ? <Text style={styles.error}>{authError}</Text> : null}

        <Button
          title={signingIn ? "Signing in…" : "Sign in with Google"}
          onPress={handleSignIn}
          disabled={signingIn}
        />

        <Text style={styles.note}>
          Uses your existing Google account — same user as the web app, so
          races you've already created are accessible here.
        </Text>
        <StatusBar style="light" />
      </ScrollView>
    );
  }

  return (
    <>
      <AuthedShell user={user} onSignOut={handleSignOut} />
      <StatusBar style="light" />
    </>
  );
}

// ── Authed shell: screen state machine + recorder lifetime ─────────────
function AuthedShell({
  user,
  onSignOut,
}: {
  user: User;
  onSignOut: () => void;
}) {
  const [selectedRace, setSelectedRace] = useState<Race | null>(null);

  // Recorder lives at this level — its lifetime spans the signed-in
  // session, not a particular screen. raceId is sourced from the
  // selected race; when no race is selected it's null and the hook
  // refuses to start (see useTrackRecorder.start).
  const recorder = useTrackRecorder(selectedRace?.id ?? null);

  // Request notification permission once on first authed mount. Idempotent
  // — if already granted/denied the OS does not re-prompt. A denial
  // silently disables the T-6 notification path; the T-5 BackgroundFetch
  // fallback still works.
  useEffect(() => {
    void requestNotificationPermission();
  }, []);

  const handleStart = async () => {
    await requestBatteryOptimizationExemption();
    await recorder.start();
  };

  // Defensive: if the recorder is mid-recording and the user somehow
  // ends up back at the picker (UI prevents this via disabled Back
  // button, but belt-and-braces), the recorder keeps running and the
  // raceId stays valid via raceIdRef inside the hook. We block clearing
  // the selection while recording so the user can't strand the recorder
  // on a null raceId.
  const handleBackToPicker = () => {
    if (recorder.recording) return; // no-op
    setSelectedRace(null);
  };

  if (!selectedRace) {
    return (
      <RacePickerScreen
        userEmail={user.email}
        onSelect={setSelectedRace}
        onSignOut={onSignOut}
      />
    );
  }

  return (
    <RecorderScreen
      race={selectedRace}
      recorder={recorder}
      onBack={handleBackToPicker}
      onStart={handleStart}
    />
  );
}

const styles = StyleSheet.create({
  container: {
    flexGrow: 1,
    backgroundColor: "#0b1f2a",
    paddingTop: 72,
    paddingHorizontal: 24,
    gap: 14,
  },
  centered: { alignItems: "center", justifyContent: "center" },
  title: { color: "#f5f7fa", fontSize: 26, fontWeight: "700" },
  subtitle: { color: "#8fb4c7", fontSize: 14, marginBottom: 12 },
  error: { color: "#e08a8a", fontSize: 13 },
  note: { color: "#5e7d8c", fontSize: 12, marginTop: 20 },
});
