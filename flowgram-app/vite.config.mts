/**
 * Standalone web build of the FlowGram desktop UI, vendored for RELION.
 *
 * Differences from upstream (see VENDOR.md):
 *  - no Tauri shell; the browser talks to the RELION agent directly
 *  - FlowGram's own API endpoints are answered by `bridge/middleware.ts` from
 *    the RELION agent, so neither side has to learn the other's vocabulary
 */

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

import { bridge } from './bridge/middleware';

// Where the RELION agent runs. scripts/dev_flowgram.sh sets this when it
// starts the agent on a non-default port.
const AGENT = process.env.RELION_WEB_API ?? 'http://127.0.0.1:8765';

/**
 * FlowGram's endpoints, answered from the RELION agent.
 *
 * Registered before the proxy below: `use` middleware runs first, so a request
 * the bridge recognises never reaches the agent's own (differently shaped)
 * routes, and everything it does not recognise falls through to the proxy.
 */
function relionBridge() {
  return {
    name: 'relion-bridge',
    configureServer(server: { middlewares: { use: (fn: unknown) => void } }) {
      server.middlewares.use(bridge());
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), relionBridge()],
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
