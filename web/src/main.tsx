import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RouterProvider } from 'react-router';

import { router } from './router';
import './index.css';

/**
 * The query client is created once, at module scope, and is the ONE cache for server data in this
 * application (ADR-0001). Nothing copies server data into `useState`.
 *
 * The retry policy is set here rather than per query because it is a product decision, not a
 * technical one: the tailoring call costs money per attempt, so silently retrying three times —
 * TanStack Query's default — would triple the bill for a user who is about to see an error anyway.
 * A query that genuinely wants retries asks for them explicitly.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
});

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
