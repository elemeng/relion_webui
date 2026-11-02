/**
 * Standalone web build of the FlowGram desktop UI, vendored for RELION.
 *
 * Differences from upstream (see VENDOR.md):
 *  - no Tauri shell; the browser talks to the RELION agent directly
 *  - the dev server proxies /api and /ws to that agent instead of reading a
 *    connection.json written by a spawned Rust backend
 */

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Where the RELION agent runs. scripts/dev_flowgram.sh sets this when it
// starts the agent on a non-default port.
const AGENT = process.env.RELION_WEB_API ?? 'http://127.0.0.1:8765';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    dedupe: ['react', 'react-dom'],
  },
  server: {
    host: '127.0.0.1',
    port: 1420,
    strictPort: true,
    proxy: {
      '/api': { target: AGENT, changeOrigin: false },
      '/ws': { target: AGENT.replace(/^http/, 'ws'), ws: true },
    },
  },
  build: {
    target: 'es2022',
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-react': ['react', 'react-dom', 'react-dom/client', 'react/jsx-runtime'],
          'vendor-markdown': ['react-markdown', 'remark-gfm'],
        },
      },
    },
  },
});
