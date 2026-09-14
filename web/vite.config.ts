import { fileURLToPath, URL } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
// `vitest/config`, not `vite`: its `defineConfig` knows the `test` block. The triple-slash
// reference this file used to carry did not augment the type, which went unnoticed while the
// tsc gate checked zero files.
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: true,
    port: 5173,
    // Vite runs behind nginx in dev (docker/nginx/dev.conf) so that http://localhost:8080 behaves
    // the way the deployed app does — same origin for the SPA and /api, which is what makes cookie
    // behaviour faithful. HMR needs to be told the port it is reached on, or the browser tries to
    // open a WebSocket back to a port nginx is not listening on and hot reload silently dies.
    hmr: { clientPort: 8080 },
    // Direct access on :5173 still needs to reach the API; through nginx this proxy is unused.
    proxy: {
      '/api': { target: 'http://api:8000', changeOrigin: true },
      '/health': { target: 'http://api:8000', changeOrigin: true },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
});
