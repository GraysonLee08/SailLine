// Metro config for using Expo inside the npm-workspaces monorepo.
// Without this, Metro won't find @sailline/shared (hoisted to the repo
// root) and will throw "Unable to resolve module". This is the standard
// Expo monorepo setup.

// 2026-06-03 — release-build monorepo fix.
//
// Expo's metro-config calls `getMetroServerRoot(projectRoot)`, which by
// default walks UP from `mobile/` to find the workspaces root and uses
// THAT as Metro's `unstable_serverRoot`. Metro's `_resolveRelativePath`
// then resolves the embed entry (`./index.js`) from `${serverRoot}/.`
// — i.e. `<workspace>/.` — which has no index.js, and `gradlew
// assembleRelease` fails with "Unable to resolve module ./index.js from
// E:\Personal\Coding\SailLine/.".
//
// Setting `EXPO_NO_METRO_WORKSPACE_ROOT=1` BEFORE requiring
// `expo/metro-config` short-circuits `getMetroServerRoot` so it returns
// `projectRoot` directly. serverRoot stays at `mobile/`, the entry
// resolves against the real `mobile/index.js` shim, and the bundle
// task completes. Cross-package imports from `@sailline/shared` keep
// working because the resolution there goes through node_modules
// (workspace-hoisted) + the explicit `nodeModulesPaths` we set below,
// neither of which is affected by serverRoot.
//
// Debug builds were unaffected because they skip the embed step and
// load the JS bundle from the dev Metro server at runtime.
//
// Must be set before any `require("expo/metro-config")` and before any
// `require("expo/...")` that transitively loads metro-config.
process.env.EXPO_NO_METRO_WORKSPACE_ROOT = "1";

const { getDefaultConfig } = require("expo/metro-config");
const path = require("path");

const projectRoot = __dirname;
const workspaceRoot = path.resolve(projectRoot, "..");

const config = getDefaultConfig(projectRoot);

// 1. Watch all files in the monorepo (so changes to packages/shared hot-reload).
//    Preserve Expo's default watchFolders and ADD the workspace root, rather
//    than replacing them (expo-doctor flags a bare replacement).
config.watchFolders = [
  ...(config.watchFolders ?? []),
  workspaceRoot,
];

// 2. Resolve modules from this app first, then the hoisted root node_modules.
config.resolver.nodeModulesPaths = [
  path.resolve(projectRoot, "node_modules"),
  path.resolve(workspaceRoot, "node_modules"),
];

// 3. Force `react` + `react-native` to mobile's copy regardless of where
//    they're imported from.
//
// Background: after Phase-3, expo-doctor showed two Reacts in the tree:
//   react@19.1.0 at mobile/node_modules/react       (RN 0.81 wants this)
//   react@18.3.1 at <root>/node_modules/react       (Vite frontend pins this)
// The frontend's React 18 hoists to the workspace root. When a package
// hoisted to root (e.g. @react-navigation/core, which expo-router pulls
// in) does `require("react")`, Metro's hierarchical lookup walks UP from
// the root and finds the root's 18.3.1 — yielding two copies in the
// same bundle and the "Invalid hook call" crash.
//
// `disableHierarchicalLookup: true` was the obvious fix but cascades
// into "transitive X not found" errors for every dep that lived in a
// nested node_modules folder (expo-asset, @expo/metro-runtime,
// @react-native/virtualized-lists, ...) — exhausting whack-a-mole.
//
// `resolveRequest` is Metro's override hook (extraNodeModules is only a
// FALLBACK, used after normal resolution fails — it can't fix duplicate
// resolutions). Forcing the resolution of just `react` + `react-native`
// + `scheduler` (the common React-internal peer that also dupes) keeps
// every other transitive resolving normally via hierarchical lookup.
// Only `react` is actually duplicated in this monorepo (frontend pins
// 18.3.1, mobile pins 19.1.0). `react-native` lives only in mobile (web
// uses Vite, not Metro), and `scheduler` is a single hoisted copy at
// the workspace root — forcing them to mobile would create new "module
// not found" errors. Keep the override surgical.
const FORCE_MOBILE = ["react"];

config.resolver.resolveRequest = (context, moduleName, platform) => {
  // Match exact package or subpath (e.g. "react/jsx-runtime").
  const base = moduleName.split("/", 1)[0];
  if (FORCE_MOBILE.includes(base)) {
    const remainder = moduleName.slice(base.length); // "" or "/sub/path"
    const forcedPath = path.join(projectRoot, "node_modules", base) + remainder;
    return context.resolveRequest(context, forcedPath, platform);
  }
  return context.resolveRequest(context, moduleName, platform);
};

module.exports = config;
