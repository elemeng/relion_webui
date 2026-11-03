/**
 * Standalone web build of the FlowGram desktop UI, vendored for RELION.
 *
 * The app is pure static files: every API call goes to the RELION agent's own
 * `/api`, and the agent serves this build itself (see
 * `backend/relion_webui/flowgram/`). In development Vite proxies `/api` and
 * `/mcp` so hot reload works without a build step.
 */
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Where the RELION agent runs during development.
const AGENT = process.env.RELION_WEB_API ?? 'http://127.0.0.1:8765';

export default defineConfig({
  // The agent serves this build at its root.
  base: '/',
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
      '/mcp': { target: AGENT, changeOrigin: false },
    },
  },
  build: {
    // Straight into the agent's package, so `uvicorn` alone serves the canvas.
    outDir: '../backend/relion_webui/static',
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
