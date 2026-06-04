// permissions.ts — request the OS-level permissions the recorder needs.
//
// Pairs with the PermissionWelcomeCard (src/components/PermissionWelcomeCard.tsx),
// which is shown once on first launch after sign-in. The card calls
// requestRecorderPermissions() and surfaces the per-permission result so
// the user can see which ones they granted vs declined.
//
// Three permission domains, each independently optional. The recorder
// degrades gracefully when one is missing:
//
//   * Location (foreground + Always) — without it, the blue user dot
//     doesn't render and the "centre on me" FAB is functionally
//     useless. Background ("Always") is what keeps GPS flowing when
//     the phone is locked in the sailor's pocket; without it the
//     recorder pauses every time the screen times out.
//
//   * Activity recognition (motion / pedometer) — used by Transistorsoft
//     to detect when the boat is genuinely stationary so it can throttle
//     polling and save battery. The recorder still works without it;
//     battery drain just goes up.
//
//   * Notifications — race-start reminders (T-6 min), missed-mark
//     prompts, and route-update alerts all ride on this. Without it
//     those features silently no-op.
//
// IMPORTANT: this module wraps Transistorsoft's BackgroundGeolocation
// permission API (NOT expo-location). Transistorsoft is the source of
// truth for the location permission state since it's what we use to
// actually capture fixes — expo-location would prompt for the same
// thing but the SDK would still think it was unauthorized.
//
// Idempotency: every helper is a no-op when permission is already
// granted. Safe to call repeatedly (e.g. on every cold start).
//
// 2026-06-04 user feedback: previous flow only requested notifications
// at sign-in; location was prompted lazily by BackgroundGeolocation.ready()
// when recording started, and activity recognition was never explicitly
// requested. The user saw the notifications prompt and assumed nothing
// else needed enabling. This module makes the three-pronged ask
// explicit and up-front.

import BackgroundGeolocation from "react-native-background-geolocation";
import * as Pedometer from "expo-sensors/build/Pedometer";

import { requestNotificationPermission } from "./scheduledAutoStart";

/** Tri-state result so the welcome card can show a per-row badge. */
export type PermissionResult = "granted" | "denied" | "unavailable";

export type RecorderPermissionStatus = {
  notifications: PermissionResult;
  /**
   * "granted" = Always (background). "denied" = WhenInUse or denied —
   * recording will pause when the screen sleeps.
   * For our use case "WhenInUse" is functionally equivalent to denied
   * (the whole point of the recorder is background capture), so we
   * collapse both into "denied" with the card explaining the impact.
   */
  locationAlways: PermissionResult;
  /** Activity / motion / pedometer — battery optimisation only. */
  activity: PermissionResult;
};

/**
 * Ask Transistorsoft to request Always-location authorization.
 *
 * v5 API: BackgroundGeolocation.requestPermission() returns the
 * authorization status as an integer. The valid values live on the
 * ``AuthorizationStatus`` enum (ALWAYS, WHEN_IN_USE, RESTRICTED,
 * DENIED, NOT_DETERMINED). Only ALWAYS gives us screen-locked
 * recording; everything else collapses to "denied" for our purposes
 * (a WhenInUse grant still pauses the recorder when the screen
 * times out).
 *
 * v4 named these constants ``AUTHORIZATION_STATUS_*`` as flat
 * properties on the BackgroundGeolocation object — that shape is
 * gone in v5 and the TS types will reject it.
 */
async function requestLocationPermission(): Promise<PermissionResult> {
  try {
    const status = await BackgroundGeolocation.requestPermission();
    if (status === BackgroundGeolocation.AuthorizationStatus.Always) {
      return "granted";
    }
    return "denied";
  } catch (e) {
    // Simulator / unavailable SDK path. Don't crash the welcome card.
    return "unavailable";
  }
}

/**
 * Ask expo-sensors for Pedometer (motion / activity recognition)
 * authorization. Required on iOS (Motion & Fitness toggle) and on
 * Android API 29+ (ACTIVITY_RECOGNITION runtime permission).
 *
 * Pedometer is the public surface for the underlying activity-
 * recognition data we never actually consume directly — Transistorsoft
 * does. Granting it here is what lets Transistorsoft's stationary
 * detection throttle GPS polling when the boat is moored.
 */
async function requestActivityPermission(): Promise<PermissionResult> {
  try {
    const existing = await Pedometer.getPermissionsAsync();
    if (existing.status === "granted") return "granted";
    if (!existing.canAskAgain) return "denied";
    const response = await Pedometer.requestPermissionsAsync();
    return response.status === "granted" ? "granted" : "denied";
  } catch (e) {
    return "unavailable";
  }
}

/**
 * Run the notification permission request — same function the existing
 * gate has been calling, just re-exposed here so the welcome card can
 * report the result alongside the other two.
 */
async function requestNotifications(): Promise<PermissionResult> {
  try {
    const granted = await requestNotificationPermission();
    return granted ? "granted" : "denied";
  } catch (e) {
    return "unavailable";
  }
}

/**
 * Run all three prompts in sequence, returning the per-domain status.
 *
 * Sequence is intentional: notifications first (lightest, sets the tone
 * with a familiar prompt), then location (the load-bearing one), then
 * activity (the optional battery saver). Each step waits for the user
 * to dismiss the OS prompt before the next one fires; no concurrent
 * prompts get stacked on top of each other.
 *
 * The function never throws — every failure mode is reported in the
 * returned status. Callers can render a per-row badge from this.
 */
export async function requestRecorderPermissions(): Promise<RecorderPermissionStatus> {
  const notifications = await requestNotifications();
  const locationAlways = await requestLocationPermission();
  const activity = await requestActivityPermission();
  return { notifications, locationAlways, activity };
}

/**
 * Read the current state without prompting. Used by the Settings
 * screen to show which permissions are granted and which need fixing
 * via system Settings.
 *
 * Returns the same shape as requestRecorderPermissions, so a UI can be
 * driven off either function interchangeably.
 */
export async function getRecorderPermissionStatus(): Promise<RecorderPermissionStatus> {
  let notifications: PermissionResult = "unavailable";
  let locationAlways: PermissionResult = "unavailable";
  let activity: PermissionResult = "unavailable";

  try {
    // We can't read notifications without importing the module here;
    // the underlying call is cheap (no UI, just a permission lookup).
    const Notifications = await import("expo-notifications");
    const existing = await Notifications.getPermissionsAsync();
    notifications = existing.status === "granted" ? "granted" : "denied";
  } catch {
    /* unavailable on this build */
  }

  try {
    const state = await BackgroundGeolocation.getProviderState();
    // ProviderState.status: 0 = NotDetermined, 1 = Restricted, 2 = Denied,
    // 3 = Always, 4 = WhenInUse. Always is "granted" for our purposes.
    // (Same v4 → v5 enum rename as requestLocationPermission — see note
    // there.)
    locationAlways =
      state?.status === BackgroundGeolocation.AuthorizationStatus.Always
        ? "granted"
        : "denied";
  } catch {
    /* unavailable */
  }

  try {
    const existing = await Pedometer.getPermissionsAsync();
    activity = existing.status === "granted" ? "granted" : "denied";
  } catch {
    /* unavailable */
  }

  return { notifications, locationAlways, activity };
}
