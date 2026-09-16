import { fileURLToPath, URL } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
// `vitest/config`, not `vite`: its `defineConfig` knows the `test` block. The triple-slash
// reference this file used to carry did not augment the type, which went unnoticed while the
// tsc gate checked zero files.
import { defineConfig } from 'vitest/config';

/**
 * The editor's dependency families, each with the small packages only it pulls in, keyed by the
 * chunk they go into (see `manualChunks` below). A module id under `node_modules/` whose package
 * path starts with a prefix here lands in that chunk; everything else takes Rollup's default.
 */
const VENDOR_CHUNKS: ReadonlyArray<readonly [chunk: string, prefixes: readonly string[]]> = [
  ['tiptap', ['@tiptap/', 'fast-equals/', 'linkifyjs/']],
  ['prosemirror', ['prosemirror-', 'orderedmap/', 'w3c-keyname/', 'rope-sequence/']],
  ['markdown', ['markdown-it/', 'linkify-it/', 'mdurl/', 'uc.micro/', 'entities/', 'punycode.js/']],
];

/**
 * Which chunk a `node_modules` module belongs in, or `undefined` for Rollup's default.
 *
 * Slice 1.4 brought the first heavy dependencies into `web/`: TipTap, ProseMirror and markdown-it
 * roughly triple the bundle (≈ 236 kB → ≈ 895 kB minified) and Vite warns past 500 kB in one
 * chunk. The split is **by package family, not by size**: the three families change on their own
 * schedules (TipTap releases often, ProseMirror rarely, markdown-it almost never), the application
 * changes every deploy, and a browser that has cached a vendor chunk keeps it across releases that
 * touch only our code. These are static chunks, so the first visit still downloads all of them —
 * deferring the editor until a run succeeds would be a `React.lazy` in `RunPage`, which is a
 * different decision (a Suspense fallback and a chunk-load failure path) and is not made here.
 */
function chunkOf(id: string): string | undefined {
  const marker = '/node_modules/';
  const at = id.lastIndexOf(marker);
  if (at === -1) {
    return undefined;
  }
  const packagePath = id.slice(at + marker.length);
  return VENDOR_CHUNKS.find(([, prefixes]) =>
    prefixes.some((prefix) => packagePath.startsWith(prefix)),
  )?.[0];
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    rollupOptions: { output: { manualChunks: chunkOf } },
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
