/**
 * Standalone web build of the FlowGram desktop UI, vendored for RELION.
 *
 * The app is pure static files: every API call goes to the RELION agent under
 * `/fg`, which serves this build itself in production (see
 * `backend/relion_webui/flowgram/`). In development Vite proxies `/fg` to the
 * agent so hot reload works without a build step.
 */
import { defineConfig } from 'vite';
import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Where the RELION agent runs during development.
const AGENT = process.env.RELION_WEB_API ?? 'http://127.0.0.1:8765';

export default defineConfig({
  // Assets are served from /fg/, so the bundle must not assume it owns the root.
  base: '/fg/',
  plugins: [react(), tailwindcss()],
  resolve: {
    dedupe: ['react', 'react-dom'],
    alias: {
      // The main branch's layout engine is not imported any more - the agent
      // projects the pipeline and lays it out in Python. Kept out on purpose:
      // one implementation, not two that can drift.
      '@': fileURLToPath(new URL('../frontend/src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 1420,
    strictPort: true,
    proxy: {
      '/fg': { target: AGENT, changeOrigin: false },
    },
  },
  build: {
    // Straight into the agent's package, so `uvicorn` alone serves the canvas.
    outDir: '../backend/relion_webui/flowgram_static',
    emptyOutDir: true,
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
