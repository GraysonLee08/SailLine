# 2026-06-03 — Mobile Google sign-in fix (DEVELOPER_ERROR code 10)

## What we worked on

Diagnosed and fixed a `Sign-in failed (10): DEVELOPER_ERROR` on the mobile
app's Google sign-in screen. Root cause was a SHA-1 fingerprint mismatch
between the keystore signing the current local-Gradle build and the SHA-1s
registered on the Android OAuth client in Google Cloud Console.

The 2026-06-02 pivot off EAS to local `expo prebuild` switched the signing
key to `android/app/debug.keystore` (the stock default Android debug key,
SHA-1 `5E:8F:16:06:2E:A3:CD:2C:4A:0D:54:78:76:BA:A6:F3:8C:AB:F6:25`).
The OAuth client only had the prior EAS-era fingerprint
(`6C:A1:B3:A3:5D:17:C4:5E:62:E5:93:44:60:3B:09:54:B5:F5:75:21`) registered,
so Google's token issuer rejected the sign-in.

The `webClientId`, the `androidClientId`, the package name
(`com.sailline.app`), and the absence of `google-services.json` (we use the
Firebase JS SDK + `signInWithCredential`, not native Firebase Android) were
all fine — purely a registration gap.

## Files changed

- `mobile/src/googleAuthConfig.ts` — rewrote the setup-notes comment to
  list both registered SHA-1s, label which one comes from the local
  prebuild debug keystore, and call out the code-10 DEVELOPER_ERROR
  symptom so the next debugger doesn't go down the same path.

## Console action (manual, outside the repo)

- Added the local debug SHA-1
  `5E:8F:16:06:2E:A3:CD:2C:4A:0D:54:78:76:BA:A6:F3:8C:AB:F6:25` as a
  second fingerprint on the existing Android OAuth client
  (`…hrjcl1crmrsv7lcam7dj8l0ei1ggfc8c`) in the `sailline` Google Cloud
  project. Original EAS-era SHA-1 left in place.

## Decisions and rationale

- **Add a second fingerprint, not replace.** Android OAuth clients
  support multiple SHA-1s. Adding lets old EAS-signed builds keep
  working if they're ever resurrected, costs nothing.
- **Did not swap local keystore for the EAS-managed one.** We
  intentionally moved off EAS yesterday; pulling that keystore back in
  would re-couple the build pipeline to `eas credentials`. A second
  registered fingerprint achieves the same outcome without the
  dependency.
- **Did not touch `google-services.json`.** Mobile auth runs through the
  Firebase JS SDK, which only needs the web client ID as audience. No
  native Firebase Android SDK is wired up, so the file isn't required.

## Open items / next steps

- Update the registered SHA-1s when a real **release** keystore is
  introduced (the `keystore.properties` plan from 2026-06-02). The
  release cert will have its own SHA-1 — add it as a third fingerprint
  on the same Android OAuth client at that time.
- Until then, `android/app/build.gradle` release `buildType` is still
  reusing the debug signingConfig. Fine for dev, must change before any
  store submission.

## Technical debt flagged

- **No release signing config yet.** `signingConfigs.release` doesn't
  exist; the `release` buildType reads `signingConfigs.debug` (build.gradle:127).
  Already noted in the 2026-06-02 prebuild pivot memory — surfaced again
  here so it's not lost.
- **Setup-note comments drift.** Pre-fix, the comment in
  `googleAuthConfig.ts` claimed the EAS dev keystore SHA-1 was
  registered. The actually-registered SHA-1 was a different value
  (`6C:A1:…:75:21`). Comments in this file are not authoritative;
  Google Cloud Console is. Worth a periodic reconciliation.
