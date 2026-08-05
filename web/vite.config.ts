import { resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Three apps share this project:
//   index.html — developer agent workspace, served by oc_codex_server.py (47313)
//   chat.html  — consumer assistant,       served by oc_chat_server.py  (47292)
//   plain.html — pure conversation,        served by oc_web_server.py   (47291)
// `npm run dev` proxies to whichever backend OC_BACKEND points at; `npm run
// build` emits both into web/dist, which each Python server serves itself, so
// production needs no Node process at all.
const BACKEND = process.env.OC_BACKEND ?? "http://127.0.0.1:47313";

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
    rollupOptions: {
      input: {
        index: resolve(__dirname, "index.html"),
        chat: resolve(__dirname, "chat.html"),
        plain: resolve(__dirname, "plain.html"),
      },
    },
  },
});
