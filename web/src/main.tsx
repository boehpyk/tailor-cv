import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
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

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
