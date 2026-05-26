// firebase.ts — Firebase init for the mobile app (RN).
//
// Mirrors frontend/src/firebase.js (same project; config keys are public
// identifiers, not secrets). The one RN-specific difference: auth state
// must be persisted with AsyncStorage, otherwise the user is signed out
// on every cold start. We use initializeAuth + getReactNativePersistence
// instead of the web's getAuth().
//
// Phase 1 only needs a valid ID token so apiFetch can authenticate the
// telemetry POSTs; the full login UI is Phase 2. The test-harness screen
// (App.tsx) provides a minimal email/password sign-in for now.

import ReactNativeAsyncStorage from "@react-native-async-storage/async-storage";
import { initializeApp } from "firebase/app";
// @ts-expect-error — getReactNativePersistence is exported by the RN
// build of firebase/auth but is missing from the published type defs.
import { getReactNativePersistence, initializeAuth } from "firebase/auth";

const firebaseConfig = {
  apiKey: "AIzaSyDQCeMfB1EOMwRNCExXO52g11J2ynYdtRM",
  authDomain: "sailline.firebaseapp.com",
  projectId: "sailline",
  storageBucket: "sailline.firebasestorage.app",
  messagingSenderId: "105706282249",
  appId: "1:105706282249:web:a807ee7f63f041705e87d8",
  measurementId: "G-RC0G8DL7BX",
};

export const app = initializeApp(firebaseConfig);

export const auth = initializeAuth(app, {
  persistence: getReactNativePersistence(ReactNativeAsyncStorage),
});
