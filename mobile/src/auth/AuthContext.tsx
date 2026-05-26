// AuthContext.tsx — Firebase user + Google sign-in plumbing.
//
// Extracted from the old App.tsx so that expo-router's layouts can read
// auth state via context. Layout files render before any screen, so this
// is the natural place for the auth gate.
//
// Two concerns kept together:
//   1. Listening for Firebase auth-state changes (single onAuthStateChanged
//      subscription per app process).
//   2. The actual sign-in / sign-out actions (native Google Play Services
//      → Firebase credential exchange).
//
// Why one provider, not two: the actions and the state share refs to the
// `auth` object and reading them together avoids prop-drilling through
// the layout tree. Splitting into AuthProvider + AuthActionsProvider
// would be premature.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

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

import { auth } from "../firebase";

export type AuthState = {
  /** True until the first onAuthStateChanged fires. */
  ready: boolean;
  /** Firebase user or null when signed out. */
  user: User | null;
  /** Surface of the most recent sign-in error (if any). */
  error: string | null;
  /** True while a sign-in is in flight (after the user tapped, before resolve). */
  signingIn: boolean;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
  clearError: () => void;
};

const AuthCtx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signingIn, setSigningIn] = useState(false);

  useEffect(() => {
    const unsub = onAuthStateChanged(auth, (u) => {
      setUser(u);
      setReady(true);
    });
    return unsub;
  }, []);

  const signIn = useCallback(async () => {
    setSigningIn(true);
    setError(null);
    try {
      await GoogleSignin.hasPlayServices({
        showPlayServicesUpdateDialog: true,
      });
      const response = await GoogleSignin.signIn();
      if (!isSuccessResponse(response)) {
        // User cancelled the system sheet — no-op, no error.
        setSigningIn(false);
        return;
      }
      const idToken = response.data.idToken;
      if (!idToken) {
        setError("Google sign-in returned no idToken.");
        setSigningIn(false);
        return;
      }
      const credential = GoogleAuthProvider.credential(idToken);
      await signInWithCredential(auth, credential);
    } catch (e) {
      if (isErrorWithCode(e)) {
        switch (e.code) {
          case statusCodes.SIGN_IN_CANCELLED:
            break; // not an error, user backed out
          case statusCodes.IN_PROGRESS:
            setError("Sign-in already in progress.");
            break;
          case statusCodes.PLAY_SERVICES_NOT_AVAILABLE:
            setError("Google Play Services not available on this device.");
            break;
          default:
            setError(`Sign-in failed (${e.code}): ${e.message}`);
        }
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setSigningIn(false);
    }
  }, []);

  const doSignOut = useCallback(async () => {
    try {
      await GoogleSignin.signOut();
    } catch {
      /* best effort */
    }
    await signOut(auth);
  }, []);

  const clearError = useCallback(() => setError(null), []);

  const value = useMemo<AuthState>(
    () => ({
      ready,
      user,
      error,
      signingIn,
      signIn,
      signOut: doSignOut,
      clearError,
    }),
    [ready, user, error, signingIn, signIn, doSignOut, clearError],
  );

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

/**
 * Read the auth context. Throws if called outside <AuthProvider>; this is
 * a programming error, not a recoverable state — the layout always wraps.
 */
export function useAuth(): AuthState {
  const v = useContext(AuthCtx);
  if (!v) throw new Error("useAuth must be used inside <AuthProvider>");
  return v;
}
