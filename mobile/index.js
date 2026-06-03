// Mobile app entry — re-exports expo-router/entry.
//
// Why this file exists (2026-06-02 prebuild pivot):
//   When `package.json` has `"main": "expo-router/entry"`, dev Metro
//   finds it via watchFolders + nodeModulesPaths fine. But the release
//   bundler (`expo export:embed`) resolves the main field as a relative
//   path against whatever it picks as the project root. In a monorepo
//   with hoisted node_modules, it picks the workspace root and looks
//   for `./node_modules/expo-router/entry.js` which doesn't exist there
//   (expo-router lives in `mobile/node_modules`).
//
// Re-exporting through a local file makes the entry an unambiguous
// path relative to mobile/, which works in both dev and release.
import "expo-router/entry";
