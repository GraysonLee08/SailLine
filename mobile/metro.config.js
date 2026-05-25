// Metro config for using Expo inside the npm-workspaces monorepo.
// Without this, Metro won't find @sailline/shared (hoisted to the repo
// root) and will throw "Unable to resolve module". This is the standard
// Expo monorepo setup.
const { getDefaultConfig } = require("expo/metro-config");
const path = require("path");

const projectRoot = __dirname;
const workspaceRoot = path.resolve(projectRoot, "..");

const config = getDefaultConfig(projectRoot);

// 1. Watch all files in the monorepo (so changes to packages/shared hot-reload).
config.watchFolders = [workspaceRoot];

// 2. Resolve modules from this app first, then the hoisted root node_modules.
//    Hierarchical lookup is intentionally LEFT ENABLED (Metro's default): npm
//    workspaces hoist most deps to the root, but expo's transitive deps (e.g.
//    expo-asset) may sit at various levels, and disabling the walk-up breaks
//    their resolution. npm workspaces keep a single hoisted React, so the
//    usual "duplicate React" reason for disabling lookup doesn't apply here.
config.resolver.nodeModulesPaths = [
  path.resolve(projectRoot, "node_modules"),
  path.resolve(workspaceRoot, "node_modules"),
];

module.exports = config;
