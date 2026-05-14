# Android (Capacitor) Setup Runbook

**Audience:** solo dev on Windows / PowerShell.
**Status:** first-time setup. Run end-to-end exactly once. Subsequent dev work is `npm run build && npx cap sync android` per change.

The web build path is untouched by anything in this runbook. `npm run dev`, `npm run build`, and `npm run deploy` continue to work in the browser.

---

## 0. Prerequisites (one-time, host machine)

1. **Android Studio** (latest stable) — installs the Android SDK and `adb`.
   - During setup, accept the default SDK location. Note the path (typical: `C:\Users\grayv\AppData\Local\Android\Sdk`).
2. **JDK 17** — Android Gradle Plugin 8.x requires it. Android Studio bundles one; verify with `java -version` from PowerShell.
3. **A physical Android phone with USB debugging enabled** — Settings → About phone → tap *Build number* 7 times → Developer options → enable USB debugging.
4. Confirm `adb` sees the phone: `adb devices`.

---

## 1. Install Capacitor + the background-geolocation plugin

Run from `frontend/` in PowerShell:

```powershell
cd E:\Personal\Coding\SailLine\frontend
npm install @capacitor/core @capacitor/android @capacitor/geolocation @capacitor-community/background-geolocation
npm install --save-dev @capacitor/cli
```

Why these:
- `@capacitor/core` — runtime bridge between WebView and native code.
- `@capacitor/cli` — `npx cap` commands (dev-only).
- `@capacitor/android` — the Android native project template.
- `@capacitor/geolocation` — foreground geolocation (kept for symmetry / future use; the recorder uses background plugin).
- `@capacitor-community/background-geolocation` — MIT-licensed background watcher with a foreground service. Free.

---

## 2. Build the web bundle

```powershell
npm run build
```

`frontend/dist/` must exist before `cap add android` (Capacitor copies it into the native project).

---

## 3. Generate the Android project

`frontend/capacitor.config.ts` already exists (committed). Do **not** run `npx cap init` — it would overwrite the config. Go straight to:

```powershell
npx cap add android
npx cap sync android
```

`cap add android` creates `frontend/android/`. `cap sync` copies the web bundle in and installs plugin native code.

Most of `frontend/android/` should be committed (it is the native project). `.gitignore` already excludes the per-machine and build artefacts.

---

## 4. Edit the Android manifest

Open `frontend/android/app/src/main/AndroidManifest.xml`.

Inside the `<manifest>` element (top, alongside the existing permissions), add:

```xml
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_BACKGROUND_LOCATION" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_LOCATION" />
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
<uses-permission android:name="android.permission.WAKE_LOCK" />
```

Inside the `<application>` element, add the plugin's foreground service (the plugin ships the class, you just declare it):

```xml
<service
    android:name="com.equimaps.capacitor_background_geolocation.BackgroundGeolocationService"
    android:foregroundServiceType="location"
    android:exported="false" />
```

(`com.equimaps.capacitor_background_geolocation.BackgroundGeolocationService` — verify the exact class path from the plugin's README before save; ship the version their docs specify.)

Save the file. Do **not** run `cap sync` again — that only copies the web bundle, not native edits.

---

## 5. Build and install on a device

```powershell
npx cap open android
```

Android Studio opens the project. Wait for the Gradle sync (first time: 5–10 min, downloads dependencies).

When sync finishes:
1. Connect the phone via USB; confirm it appears in the device dropdown (top toolbar).
2. Press the green ▶ Run button.
3. The first launch prompts for Location permission — choose **Allow all the time**.

---

## 6. Smoke test (acceptance for Session A)

The whole point of this session. Without this test passing, we have not finished Session A.

1. In the app, start a new race (or open an existing one). Begin recording.
2. Walk or drive a route for 15 minutes.
3. **Lock the screen for at least 10 of those 15 minutes.** Optional but informative: also background the app (home button) for part of the test.
4. Confirm the persistent notification "SailLine — recording track" is visible in the notification tray throughout.
5. Stop recording. Open the race in the app and verify the breadcrumb covers the whole route — no obvious gaps during the screen-locked segment.

**Pass:** continuous trail across the locked window.
**Fail:** gap in the trail aligned with the screen-lock window → escalate plugin choice (re-evaluate Transistorsoft).

---

## 7. Rebuild loop for future web changes

```powershell
cd E:\Personal\Coding\SailLine\frontend
npm run build
npx cap sync android
```

Then in Android Studio: ▶ Run.

No native changes? `cap sync` is enough; you don't need to close/reopen Android Studio.

---

## Known gotchas

- **Permission downgrade:** if the user later picks "While using the app" instead of "Allow all the time", background fixes stop. Surface this in the app UI as a future task.
- **Battery optimisation:** some OEMs (Xiaomi, OnePlus) kill foreground services aggressively. If the trail drops mid-race despite "Allow all the time", check Settings → Battery → Battery optimisation → SailLine → "Don't optimize".
- **First sync is slow:** Gradle downloads ~1 GB the first time. Subsequent syncs are seconds.
