import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite roots at the directory containing index.html (this folder).
// `dist/` is the build output that Firebase Hosting will serve.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  build: { outDir: "dist", sourcemap: true },
});
