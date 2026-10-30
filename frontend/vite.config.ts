import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// The build lands directly in the Python package's static directory so FastAPI
// serves it with no extra copy step. Assets go under `assets/` so nothing
// collides with the hand-written styles still served alongside. The built
// bundle is committed, so running the agent needs no Node toolchain.

// Where the dev server forwards API and WebSocket traffic. scripts/dev_web.sh
// sets this when the agent runs on a non-default port.
const API_TARGET = process.env.RECRYO_API ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../backend/recryo/static",
    emptyOutDir: false,
    assetsDir: "assets",
    // Fixed names keep index.html self-contained and avoid stale hash misses
    // when the build output replaces files in place.
    rollupOptions: {
      input: "index.html",
      output: {
        entryFileNames: "assets/app.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
  server: {
    // `npm run dev` proxies API calls to a running agent, so the UI can be
    // developed with hot reload instead of rebuilding for every change.
    //
    // RECRYO_API lets scripts/dev_web.sh point this at a different port;
    // the default matches the agent's own default.
    proxy: {
      "/api": API_TARGET,
      "/ws": { target: API_TARGET.replace(/^http/, "ws"), ws: true },
    },
  },
});
