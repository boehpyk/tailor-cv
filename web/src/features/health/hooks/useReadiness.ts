import { useQuery } from '@tanstack/react-query';

import { fetchReadiness, type Readiness } from '@/api/health';

export const readinessQueryKey = ['health', 'readiness'] as const;

/**
 * Poll the API's readiness report.
 *
 * A hook rather than a `useEffect` in a component, because this is *behaviour with a lifecycle* —
 * a request, a cache entry, a refetch interval and a cancellation — and that is exactly what a
 * custom hook is for. Three lines of formatting would not be.
 *
 * Note there is no `useEffect` here at all, and no `setInterval` to clean up. Polling is a property
 * of the query. That pattern reappears for the real thing this app polls: a background PDF export
 * whose job state moves from `queued` to `ready` (ADR-0005).
 */
export function useReadiness(): ReturnType<typeof useQuery<Readiness>> {
  return useQuery({
    queryKey: readinessQueryKey,
    queryFn: ({ signal }) => fetchReadiness(signal),
    refetchInterval: 15_000,
    // A 503 from this endpoint is the answer, not a failure to get one — a retry would just delay
    // the bad news the operator is asking for.
    retry: false,
  });
}
