// babel.config.js — Expo + react-native-reanimated plugin order.
//
// react-native-reanimated's worklets plugin MUST be the LAST plugin in
// the list. The expo-router preset (loaded via babel-preset-expo) handles
// the file-based-routing transforms automatically — no additional plugin
// entry is needed for it.
//
// Why a babel.config.js exists at all now: SDK 50+ defaults to a
// Metro-bundled transform that worked without this file when we had only
// the default preset, but Reanimated + bottom-sheet require the worklets
// transform.

module.exports = function (api) {
  api.cache(true);
  return {
    presets: ["babel-preset-expo"],
    plugins: ["react-native-worklets/plugin"],
  };
};
