import { QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RouterProvider } from 'react-router';

import { authStore } from './features/auth/authStore';
import { connectCrossTabSignOut, seedFromRefresh } from './features/auth/hooks/authCache';
import { createAppQueryClient } from './queryClient';
import { router } from './router';
import './index.css';

// Created once, at module scope: the one cache for server data. Defaults and why: `./queryClient`.
const queryClient = createAppQueryClient();

/**
 * Ask "is this browser logged in?" once, at module scope, before the first render (technical plan
 * §7, AC-36).
 *
 * **Not in a `useEffect`.** `<StrictMode>` runs effects twice in development, so an effect would send
 * two boot refreshes from one tab — the second presenting a token the first just rotated, which is
 * exactly the race the server's grace window exists for, triggered by our own double-mount. The
 * store's single-flight would dedupe them anyway; calling it once is the design, and single-flight
 * is the belt.
 *
 * The result seeds **this** `queryClient` — the one the provider below hands to every component —
 * so `['auth', 'me']` is already in the cache when `useAuth()` first reads it, and no `GET /me` is
 * sent to learn what the refresh response already said. `bootstrap()` never rejects: every ending
 * is an outcome (`authenticated`, `anonymous`, `unavailable`, `superseded`).
 */
void authStore.bootstrap().then((result) => {
  seedFromRefresh(queryClient, result);
});

/**
 * Hear other tabs' sign-outs (slice 2.2, AC-42) — also once, at module scope, beside the boot and
 * for the same reason: an effect would subscribe twice under `<StrictMode>`. Another tab's logout or
 * account deletion sets this tab `anonymous` and removes every `['auth', …]` query from **this**
 * `queryClient`, with no network call. Never disconnected: it lives as long as the page.
 */
connectCrossTabSignOut(queryClient);

const container = document.getElementById('root');
if (!container) {
  throw new Error('#root is missing from index.html');
}

/**
 * What React itself prints when an error boundary catches (E-29).
 *
 * `EditorErrorBoundary.componentDidCatch` already logs only the error's type — but React logs the
 * caught error **as well**, on its own, through the root's `onCaughtError`, and the default is
 * `console.error(error)` in the production build and a longer "The above error occurred in…" report
 * in development. Both print the message. A ProseMirror `RangeError` quotes the node it rejected,
 * so for the one boundary in this app the message is a line of somebody's CV, and the console is
 * the one place the spec says the document never goes. The root option is the only seat from which
 * React's own report can be replaced; it is replaced with the same thing the boundary logs: the
 * constructor's name and nothing that came from the document. `errorInfo.componentStack` is
 * deliberately not logged either — it is React's, not the document's, but the boundary already
 * says which component failed, and one line per caught error is enough.
 *
 * `name`, not `constructor.name`: a minifier renames application classes (`ApiError` → `t`) and
 * leaves `Error.prototype.name` alone, so `name` is the one that still reads in production.
 */
function onCaughtError(error: unknown): void {
  const errorType = error instanceof Error ? error.name : typeof error;
  console.error('react: an error boundary caught an error', { errorType });
}

// The router sits INSIDE the query provider: every page is a route element, and every page reads
// the cache. Loaders (the `/runs/:runId` redirect) run before any element renders, which is fine —
// no loader here touches the API; the pages fetch through hooks, not loaders (ADR-0001).
createRoot(container, { onCaughtError }).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
