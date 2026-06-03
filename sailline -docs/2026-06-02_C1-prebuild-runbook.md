# 2026-06-02 — C1 prebuild runbook (PowerShell)

**Goal:** stop EAS credit burn. Move to local `gradlew` builds installed over USB.
**You are here:** Windows + PowerShell + Android phone with USB Debugging.
**Time budget:** ~30 min if Android Studio is already installed and SDK is configured. Add ~45 min if not.

If anything errors out at a numbered step, stop and tell me the error verbatim before continuing. Don't improvise — half of these gotchas are silent.

---

## Step 0 — Verify prerequisites (one-time)

In PowerShell from anywhere:

```powershell
java -version          # expect 17.x
node -version          # expect 20+
adb --version          # part of Android SDK platform-tools
echo $env:ANDROID_HOME # should print something like C:\Users\grayv\AppData\Local\Android\Sdk
```

If `java` reports 11 or 21: install JDK 17 (Adoptium Temurin), set `JAVA_HOME` to that JDK, add `%JAVA_HOME%\bin` to PATH. RN 0.81 + Gradle 8.x specifically wants 17.

If `adb` is missing: install Android Studio, then in **SDK Manager** install **Android SDK Platform-Tools**, then add `%ANDROID_HOME%\platform-tools` to PATH.

---

## Step 1 — Set the two Gradle-side secrets (one-time per machine)

Both live in your **user-global** `~/.gradle/gradle.properties` (NOT in the repo). Prebuild regenerates the `android/` tree; anything we'd put inside it gets clobbered. User-global properties survive every prebuild forever.

```powershell
$gradleProps = "$env:USERPROFILE\.gradle\gradle.properties"
New-Item -ItemType Directory -Force -Path (Split-Path $gradleProps) | Out-Null
if (-not (Test-Path $gradleProps)) { New-Item $gradleProps -ItemType File | Out-Null }
```

Now open `$env:USERPROFILE\.gradle\gradle.properties` in Notepad and add (replacing the placeholders with your actual tokens — keep the `sk.` and the license string literally):

```properties
MAPBOX_DOWNLOADS_TOKEN=sk.PUT_YOUR_MAPBOX_DOWNLOADS_SECRET_HERE
TRANSISTOR_LICENSE=PUT_YOUR_TRANSISTORSOFT_LICENSE_KEY_HERE

# RN 0.81 with AGP 8.x: 4 GB heap, parallel + caching on. Speeds release builds.
org.gradle.jvmargs=-Xmx4096m -XX:MaxMetaspaceSize=512m
org.gradle.parallel=true
org.gradle.caching=true
```

Save. Close.

---

## Step 2 — Phone setup (one-time)

1. Settings → About Phone → tap **Build Number** 7 times. Developer Options now exposed.
2. Settings → Developer Options → enable **USB Debugging**.
3. Plug phone in via USB.
4. Back in PowerShell:

```powershell
adb devices
```

Expect a single line like `R3CW10DXYZE   device`. If it says `unauthorized` — look at your phone, accept the debugging prompt, retry. If nothing shows: most likely a USB cable that's charge-only. Swap cables.

---

## Step 3 — Pre-flight code changes (I already did these)

Before you run prebuild, the repo is staged with:

- `mobile/.gitignore` — `/android` line removed so the generated tree gets committed. `/ios` stays ignored (we're not building iOS today).
- `mobile/app.config.js` — `MAPBOX_DOWNLOAD_TOKEN` env-var path removed; the @rnmapbox/maps plugin now reads from Gradle properties directly. Transistorsoft license env-var path kept as a fallback but the primary read is now from `keystore.properties`.
- `mobile/keystore.properties.example` — template you copy to `keystore.properties` (gitignored) for the release signing key.

Nothing for you to do at this step. Just confirming what's already different in the working tree before you run prebuild.

---

## Step 4 — Run the prebuild

From `E:\Personal\Coding\SailLine\mobile`:

```powershell
cd E:\Personal\Coding\SailLine\mobile
npx expo prebuild --platform android --clean
```

`--clean` means: nuke any existing `android/` and start fresh. We want this on the first run.

Expected output: ~30 seconds. Creates `mobile/android/` with gradle, manifests, and module wiring.

If it errors with "missing plugin" — most likely `expo-build-properties` not installed. Tell me the error, I'll add a `npm install` step.

---

## Step 5 — Verify Gradle can build (no install yet)

```powershell
cd E:\Personal\Coding\SailLine\mobile\android
.\gradlew assembleDebug
```

First run downloads Gradle 8.x, the Android SDK 35 platform, and the Mapbox SDK. **Expect 5–10 minutes**, lots of "Downloading…" output. Output ends with `BUILD SUCCESSFUL`.

If it errors with `401 Unauthorized` from Mapbox: your `MAPBOX_DOWNLOADS_TOKEN` in `~/.gradle/gradle.properties` is wrong, or it's missing the `DOWNLOADS:READ` scope. Mapbox's token UI calls this "Tokens with secret scopes" — the token must start with `sk.`, NOT `pk.`.

If it errors with `Tslocationmanager` not found: same problem but for Transistorsoft — license string is wrong.

---

## Step 6 — Install on the phone, debug build

Phone still plugged in, `adb devices` still shows it:

```powershell
# Still in E:\Personal\Coding\SailLine\mobile\android
.\gradlew installDebug
```

Or, the higher-level Expo command which does prebuild + assembleDebug + install + launch Metro in one shot:

```powershell
cd E:\Personal\Coding\SailLine\mobile
npx expo run:android
```

For day-to-day iteration: use `npx expo run:android` once per session to build+install, then `npm start` for Metro and hot-reload from there.

---

## Step 7 — Release build (.apk you can hand around)

When you want a shareable installable .apk (e.g., for friends on the dock):

```powershell
cd E:\Personal\Coding\SailLine\mobile\android
.\gradlew assembleRelease
```

The .apk lands at:

```
E:\Personal\Coding\SailLine\mobile\android\app\build\outputs\apk\release\app-release.apk
```

Install on plugged-in phone:

```powershell
adb install -r E:\Personal\Coding\SailLine\mobile\android\app\build\outputs\apk\release\app-release.apk
```

`-r` means replace existing install without uninstalling user data.

**Release signing key:** the first release build will fail because there's no keystore. We'll deal with that the first time you actually want a release. Debug builds (Step 6) don't need it.

---

## Step 8 — Commit the generated tree

After Step 5 succeeds:

```powershell
cd E:\Personal\Coding\SailLine
git status                           # expect mobile/android/ as a sea of new files
git add mobile/android mobile/.gitignore mobile/app.config.js mobile/keystore.properties.example
git commit -m "chore(mobile): eject from EAS to prebuild + local Gradle"
```

Push when ready. **Do NOT commit `~/.gradle/gradle.properties` or `mobile/keystore.properties`** — both are gitignored / outside the repo on purpose.

---

## Troubleshooting

**"SDK location not found"** during gradlew: prebuild generates `android/local.properties` with the path; if it doesn't, create it manually:
```
sdk.dir=C:\\Users\\grayv\\AppData\\Local\\Android\\Sdk
```

**"Could not resolve all files for configuration ':app:debugRuntimeClasspath'" + Mapbox**: re-check `MAPBOX_DOWNLOADS_TOKEN` in `~/.gradle/gradle.properties`. Must be the secret token (`sk.`).

**"Could not find tslocationmanager:4.0.+"** with no license set: the Transistorsoft Gradle plugin treats absence of license as the open-source version that's only published to a different Maven repo. Make sure `TRANSISTOR_LICENSE` is set in `~/.gradle/gradle.properties`.

**Build succeeds but app crashes immediately on launch with "MAPBOX_ACCESS_TOKEN not set"**: separate from the download token — the runtime public token (`pk.`) is read by JS code via `process.env.EXPO_PUBLIC_MAPBOX_TOKEN`. Put it in `mobile/.env.local`:
```
EXPO_PUBLIC_MAPBOX_TOKEN=pk.your_runtime_token_here
EXPO_PUBLIC_API_URL=https://sailline-api-105706282249.us-central1.run.app
```

Metro picks `.env.local` up automatically on next start.

---

## What we lose vs EAS (acknowledged debt)

- **EAS Updates (OTA)** — `expo-updates` is still installed but the URL in `app.json` points at EAS. We can rip that out or leave it dormant. No change today.
- **EAS Submit** — irrelevant until we have a Play Store account.
- **EAS Build caching** — first local build is slow (5–10 min). Subsequent builds use Gradle's local cache and are 30–60s for incremental.

What we gain: zero EAS credit spend per build, and direct stepping with logcat / Android Studio Profiler when something misbehaves on device.

---

## After this runbook completes

Reply with: "C1 done, app launched on phone." Then I move to A1 (mark-pass reset fix). No skipping ahead.
