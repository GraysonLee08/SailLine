/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from '@vitejs/plugin-basic-ssl'

// Vite roots at the directory containing index.html (this folder).
// `dist/` is the build output that Firebase Hosting will serve.
export default defineConfig({
  plugins: [react(), basicSsl()],
  server: { port: 5173 },
  build: { outDir: "dist", sourcemap: true },

  // Vitest reads its config from the same vite.config so the test runner
  // and dev server stay in sync (plugins, aliases, etc.). jsdom gives us
  // window/WebSocket/document; setupFiles wires jest-dom matchers.
  //
  // include: Vitest's default is `**/*.{test,spec}.?(c|m)[jt]s?(x)` rooted
  // at the config's directory (frontend/). That misses our pure-JS shared
  // package tests. Explicitly include packages/shared so framework-
  // agnostic helpers (markRounding, nextMarkGuidance, telemetry, etc.)
  // get exercised in the same `npm test` run.
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.js"],
    globals: false,
    include: [
      "src/**/*.{test,spec}.?(c|m)[jt]s?(x)",
      "../packages/shared/src/**/*.{test,spec}.?(c|m)[jt]s?(x)",
    ],
  },
});
