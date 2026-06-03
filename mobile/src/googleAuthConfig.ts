// googleAuthConfig.ts — Google OAuth client IDs for the SailLine mobile app.
//
// These identify the app to Google's sign-in service. They are public:
//   - Android client IDs are gated by package name + signing-key SHA-1, so
//     a leaked ID is useless without also signing an app with the matching
//     keystore.
//   - The Web client ID is used as the Firebase "audience" when exchanging
//     a Google ID token for a Firebase credential.
//
// Setup notes:
//   - Both clients live in the same Google Cloud project that backs
//     Firebase (sailline).
//   - The Android OAuth client (com.sailline.app) is registered with
//     these SHA-1 fingerprints — multiple are allowed on one client:
//       6C:A1:B3:A3:5D:17:C4:5E:62:E5:93:44:60:3B:09:54:B5:F5:75:21
//         previously registered (pre-prebuild, likely EAS-managed)
//       5E:8F:16:06:2E:A3:CD:2C:4A:0D:54:78:76:BA:A6:F3:8C:AB:F6:25
//         android/app/debug.keystore — the local default debug key used
//         by `expo prebuild` + Gradle builds (added 2026-06-03)
//   - When we ship a real release, the release keystore will have its
//     own SHA-1 — add it as another fingerprint to this same client.
//     Symptom of a missing fingerprint: code 10 DEVELOPER_ERROR from
//     @react-native-google-signin/google-signin at sign-in.

export const GOOGLE_WEB_CLIENT_ID =
  "105706282249-v6gpoe4as4u26ur1hk9k3obli9p5dn2s.apps.googleusercontent.com";

export const GOOGLE_ANDROID_CLIENT_ID =
  "105706282249-hrjcl1crmrsv7lcam7dj8l0ei1ggfc8c.apps.googleusercontent.com";

// iOS client ID — fill in once the iOS dev client is built and an iOS
// OAuth client has been created in Google Cloud Console.
export const GOOGLE_IOS_CLIENT_ID: string | undefined = undefined;
