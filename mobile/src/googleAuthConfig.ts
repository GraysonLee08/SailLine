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
//   - The Android client is registered with SHA-1
//     EB:DD:FA:C8:A4:0F:E4:F6:BF:5A:AB:53:32:4B:41:25:90:39:7B:B0
//     which is the EAS-managed *development* keystore. When we ship to
//     production, the release keystore has a DIFFERENT SHA-1 — add it as
//     a second fingerprint to the same Android client at that point.

export const GOOGLE_WEB_CLIENT_ID =
  "105706282249-v6gpoe4as4u26ur1hk9k3obli9p5dn2s.apps.googleusercontent.com";

export const GOOGLE_ANDROID_CLIENT_ID =
  "105706282249-hrjcl1crmrsv7lcam7dj8l0ei1ggfc8c.apps.googleusercontent.com";

// iOS client ID — fill in once the iOS dev client is built and an iOS
// OAuth client has been created in Google Cloud Console.
export const GOOGLE_IOS_CLIENT_ID: string | undefined = undefined;
