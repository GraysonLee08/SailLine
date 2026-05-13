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
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.js"],
    // Keep test API explicit (imports, not globals) so editors track
    // symbols and unused-import linting works.
    globals: false,
  },
});
