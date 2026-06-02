// tokenRefresh.ts — keep Transistorsoft's native HTTP layer authed.
//
// Phase 4 of the durable upload pipeline rework
// (sailline-docs/2026-06-01_durable-upload-pipeline-plan.md).
//
// The original "we don't use native HTTP" design note flagged
// hourly-expiring Firebase ID tokens as a blocker. This module is the
// resolution: a small subscription that fetches a fresh ID token and
// pushes it into the plugin via setAuthHeader, on every event that
// could matter.
//
// Three trigger sources, layered so something always wakes us up
// before the token expires:
//
//   1. ``AppState`` foreground transition. The most reliable signal —
//      the user has the screen on, JS is awake, and a fresh token is
//      cheap.
//   2. Firebase ``onIdTokenChanged``. Fires whenever the SDK rotates
//      the token (every 55 min in foreground, plus on auth changes).
//   3. A periodic JS interval, 30 min, that only fires when JS is
//      awake. Belt-and-braces in case AppState misses a transition.
//
// Note on backgrounded sleep: if the phone is locked and JS is asleep
// when the token expires, the native plugin will start receiving 401s
// from its retries. Transistorsoft's retry/backoff strategy keeps the
// queue intact; on the next JS wake (foreground, BackgroundFetch hook,
// notification tap) we push a fresh header and call sync(). Accept
// the brief delay in upload during pure-sleep windows — we're never
// losing data.

import { AppState, type AppStateStatus } from "react-native";

import { auth } from "../firebase";

import { setAuthHeader, syncNow } from "./backgroundGeolocation";

/** How often we forcibly refresh while the app is foregrounded.
 *  30 min is well inside the 1-hour Firebase ID-token lifetime, so
 *  the native plugin never sees an expired token under steady-state
 *  foreground use. */
const FOREGROUND_REFRESH_INTERVAL_MS = 30 * 60 * 1000;

/**
 * Fetch the current user's Firebase ID token and push it into the
 * native plugin. Returns true on success, false if there's no signed-
 * in user (the recorder shouldn't be running in that case anyway).
 *
 * Calling sync() after a refresh kicks any 401-paused retries into
 * action immediately rather than waiting for the next autoSync tick —
 * helpful right after foregrounding from a long sleep.
 */
async function pushFreshToken(opts: { syncAfter: boolean }): Promise<boolean> {
  const user = auth.currentUser;
  if (!user) return false;
  try {
    // forceRefresh=false: only re-fetch if the SDK thinks the cached
    // token is close to expiry. Cheap to call on every foreground.
    const token = await user.getIdToken();
    await setAuthHeader(`Bearer ${token}`);
    if (opts.syncAfter) {
      void syncNow();
    }
    return true;
  } catch {
    // Network failure during token fetch — the native plugin will
    // continue with its previous header; we retry on the next event.
    return false;
  }
}

/**
 * Start the token-refresh subscriptions. Returns an unsubscribe
 * function suitable for a useEffect cleanup.
 *
 * Mount lifetime is the signed-in session — RecorderProvider mounts
 * this when ``auth.currentUser`` becomes non-null and unmounts on
 * sign-out so token fetches don't fire against a no-user state.
 */
export function startTokenRefresh(): () => void {
  // Initial push: the native plugin's headers may be stale if
  // recording was previously stopped and restarted. Cheap to do once.
  void pushFreshToken({ syncAfter: false });

  // Source 1 — AppState transitions to "active".
  const appStateSub = AppState.addEventListener(
    "change",
    (next: AppStateStatus) => {
      if (next === "active") {
        void pushFreshToken({ syncAfter: true });
      }
    },
  );

  // Source 2 — Firebase token rotation.
  const tokenSub = auth.onIdTokenChanged(() => {
    // onIdTokenChanged fires with the new user state, but the
    // getIdToken() call inside pushFreshToken picks up the latest
    // token regardless of the callback's arguments. Same as the
    // pattern in src/api.ts.
    void pushFreshToken({ syncAfter: false });
  });

  // Source 3 — periodic foreground refresh.
  const interval = setInterval(
    () => void pushFreshToken({ syncAfter: false }),
    FOREGROUND_REFRESH_INTERVAL_MS,
  );

  return () => {
    appStateSub.remove();
    tokenSub();
    clearInterval(interval);
  };
}
