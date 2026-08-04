import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = process.env.OC_BACKEND ?? "http://127.0.0.1:47313";

// `npm run dev` serves the app on 5173 and forwards every backend surface to a
// running Python server; `npm run build` emits into web/dist, which the Python
// server serves itself — so production needs no Node process at all.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/p": { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
  },
});
