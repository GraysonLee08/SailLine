// App.tsx — Phase 1 test harness.
//
// Minimal, intentionally ugly: just enough UI to run the 15-minute
// screen-locked acceptance test on a real device before the real
// management/auth UI lands (Phase 2). It lets you sign in (with Google,
// matching the web app's auth flow), point at a race_id, and Start/Stop
// the background recorder while watching the queue drain.
//
// Flow: sign in with Google → paste a race_id you own → Start → lock the
// screen and move for 15 min → Stop → verify the track in track_points
// (see the Phase 1 plan §5 for the SQL gap check).
//
// Auth: uses @react-native-google-signin/google-signin (native Play
// Services), then exchanges the Google ID token for a Firebase credential
// via signInWithCredential. Same Firebase user as the web app (same uid),
// so races already created with your Google account are accessible from
// the phone. The native SDK matches the app to its Google OAuth Android
// client via package name + signing-key SHA-1 — no redirect URIs to
// configure. The Web Client ID is passed only as the Firebase audience.

import { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Button,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
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
import { useTrackRecorder } from "./src/recorder/useTrackRecorder";

// Configure the Google Sign-In SDK once at module load. webClientId is
// what Firebase requires as the ID token audience — even though Android
// chooses the Android OAuth client based on package + SHA-1, the resulting
// id_token is signed with the Web Client ID as `aud` so Firebase trusts it.
GoogleSignin.configure({
  webClientId: GOOGLE_WEB_CLIENT_ID,
  offlineAccess: false,
});

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [signingIn, setSigningIn] = useState(false);

  const [raceId, setRaceId] = useState("");

  const recorder = useTrackRecorder(raceId.trim() || null);

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
      // Verify Google Play Services is available + up to date. Throws on
      // unsupported devices.
      await GoogleSignin.hasPlayServices({ showPlayServicesUpdateDialog: true });

      const response = await GoogleSignin.signIn();
      if (!isSuccessResponse(response)) {
        // User dismissed the account picker.
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
      // onAuthStateChanged picks up the result above.
    } catch (e) {
      if (isErrorWithCode(e)) {
        switch (e.code) {
          case statusCodes.SIGN_IN_CANCELLED:
            // User cancelled — no error UI needed.
            break;
          case statusCodes.IN_PROGRESS:
            setAuthError("Sign-in already in progress.");
            break;
          case statusCodes.PLAY_SERVICES_NOT_AVAILABLE:
            setAuthError(
              "Google Play Services not available on this device.",
            );
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
    // Sign out of Google so the next sign-in shows the account picker.
    try {
      await GoogleSignin.signOut();
    } catch {
      // best effort
    }
    await signOut(auth);
  };

  const handleStart = async () => {
    // Nudge the OEM battery exemption before the first locked run.
    await requestBatteryOptimizationExemption();
    await recorder.start();
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
        <Text style={styles.title}>SailLine — Phase 1 harness</Text>
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
    <ScrollView contentContainerStyle={styles.container}>
      <Text style={styles.title}>SailLine — Phase 1 harness</Text>
      <Text style={styles.subtitle}>{user.email}</Text>

      <View style={styles.row}>
        <Text style={styles.label}>race_id</Text>
        <TextInput
          style={styles.input}
          placeholder="paste a race UUID you own"
          placeholderTextColor="#5e7d8c"
          autoCapitalize="none"
          autoCorrect={false}
          editable={!recorder.recording}
          value={raceId}
          onChangeText={setRaceId}
        />
      </View>

      <View style={styles.controls}>
        {recorder.recording ? (
          <Button title="Stop recording" color="#c0392b" onPress={recorder.stop} />
        ) : (
          <Button title="Start recording" onPress={handleStart} />
        )}
      </View>

      <View style={styles.row}>
        <Text style={styles.label}>Status</Text>
        <Text style={styles.value}>
          {recorder.recording ? "RECORDING" : "stopped"}
        </Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.label}>Captured (this session)</Text>
        <Text style={styles.value}>{recorder.points.length}</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.label}>Unflushed queue</Text>
        <Text style={styles.value}>{recorder.queueLength}</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.label}>Last fix</Text>
        <Text style={styles.value}>
          {recorder.lastPoint
            ? `${recorder.lastPoint.lat.toFixed(5)}, ${recorder.lastPoint.lon.toFixed(5)}`
            : "—"}
        </Text>
      </View>
      {recorder.error ? (
        <View style={styles.row}>
          <Text style={styles.label}>Error</Text>
          <Text style={styles.error}>{recorder.error}</Text>
        </View>
      ) : null}

      <View style={styles.signOut}>
        <Button title="Sign out" color="#5e7d8c" onPress={handleSignOut} />
      </View>

      <Text style={styles.note}>
        Test: Start → lock screen → move 15 min → Stop. Then check
        track_points for gap continuity (Phase 1 plan §5).
      </Text>
      <StatusBar style="light" />
    </ScrollView>
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
  row: { backgroundColor: "#13303f", borderRadius: 10, padding: 14 },
  controls: { marginVertical: 8 },
  signOut: { marginTop: 20 },
  label: { color: "#8fb4c7", fontSize: 12, marginBottom: 4 },
  value: { color: "#f5f7fa", fontSize: 16, fontWeight: "600" },
  input: {
    backgroundColor: "#13303f",
    borderRadius: 10,
    padding: 14,
    color: "#f5f7fa",
    fontSize: 16,
  },
  error: { color: "#e08a8a", fontSize: 13 },
  note: { color: "#5e7d8c", fontSize: 12, marginTop: 20 },
});
