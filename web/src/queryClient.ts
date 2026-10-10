import { QueryClient } from '@tanstack/react-query';

/**
 * The app's query client, with the app's defaults. `main.tsx` calls this once, at module scope, and
 * the result is the ONE cache for server data in this application (ADR-0001). Nothing copies server
 * data into `useState`. It is a factory rather than an instance so a test can build a fresh client
 * that behaves like production's, instead of a hand-written one that quietly differs from it.
 *
 * The retry policy is set here rather than per query because it is a product decision, not a
 * technical one: the tailoring call costs money per attempt, so silently retrying three times —
 * TanStack Query's default — would triple the bill for a user who is about to see an error anyway.
 * A query that genuinely wants retries asks for them explicitly.
 */
export function createAppQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: 1,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
      },
    },
  });
}
