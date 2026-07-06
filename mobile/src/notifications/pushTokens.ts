// pushTokens.ts — register this device for server-initiated push (FCM).
//
// Counterpart of backend POST /api/users/me/push-tokens (migration 0024).
// Today the only server-initiated push is the dead-recorder watchdog
// (backend/workers/recorder_watchdog.py): the API notices an open race
// with no telemetry for N minutes and pushes "recording stopped" — by
// definition a moment when the app itself may be dead, which is why
// this must be a real FCM push and not a local notification.
//
// Token source: expo-notifications getDevicePushTokenAsync() — the RAW
// device token (FCM registration token on Android), NOT the Expo push
// token. The backend sends via firebase-admin messaging directly (same
// Firebase project as auth), no Expo push service in the loop.
//
// PREREQUISITE (build-time): FCM only works once google-services.json
// from the Firebase console is present at mobile/google-services.json
// and picked up by app.config.js (android.googleServicesFile). Until
// then getDevicePushTokenAsync throws and this module logs + no-ops —
// recording is unaffected, the watchdog just has nowhere to push.
//
// Registration cadence: once per app launch, after sign-in (called from
// RecorderContext alongside startTokenRefresh). The backend UPSERTs, so
// repeats are cheap and double as a last_seen_at liveness stamp. A
// module-level latch avoids hammering the endpoint on auth-state
// flapping within one JS lifetime.

import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import { apiFetch } from "../api";

let registeredThisLaunch = false;

/**
 * Fetch the device token and register it with the backend. Safe to call
 * repeatedly; only the first success per JS lifetime does work. Never
 * throws — push registration is best-effort and must not disturb the
 * caller (the recorder provider).
 */
export async function registerPushToken(): Promise<void> {
  if (registeredThisLaunch) return;
  if (Platform.OS !== "android" && Platform.OS !== "ios") return;

  try {
    const { data: token } = await Notifications.getDevicePushTokenAsync();
    if (typeof token !== "string" || token.length < 16) return;

    await apiFetch("/api/users/me/push-tokens", {
      method: "POST",
      body: { token, platform: Platform.OS },
    });
    registeredThisLaunch = true;
  } catch (e) {
    // Missing google-services.json, notification permission denied,
    // offline at launch, backend hiccup — all fine: next launch (or a
    // later call this launch) retries. Log in dev so a misconfigured
    // build is diagnosable.
    if (__DEV__) {
      // eslint-disable-next-line no-console
      console.warn(
        "[pushTokens] device push registration failed:",
        e instanceof Error ? e.message : e,
      );
    }
  }
}
