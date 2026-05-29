// featureFlags.js — single source for Vite-injected feature flags.
//
// All flags are exposed via `VITE_*` env vars (auto-injected by Vite at
// build time as `import.meta.env.VITE_*`). Read once here, export as
// plain constants, import where needed. Avoids `import.meta.env`
// sprinkled across the codebase and lets us tree-shake when a flag is
// off in production.
//
// RECORDING_ENABLED gates the webapp's on-water recording and
// calibration UI. Platform split locked 2026-05-29 (see sailline -docs/
// 2026-05-29_mobile-ui-google-maps-mapping.md §2): recording lives on
// mobile; the webapp surfaces stay only for opt-in dev use until the
// deletion PR. Default is `false` in production builds — set
// `VITE_RECORDING_ENABLED=true` in a local `.env.local` to bring the
// surfaces back during development.

export const RECORDING_ENABLED =
  import.meta.env.VITE_RECORDING_ENABLED === "true";
