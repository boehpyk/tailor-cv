import { useQuery } from '@tanstack/react-query';

import { fetchDemandCounters } from '@/api/admin';

import { adminKey } from './useAdminUser';
import { isClientError } from './useUserDirectory';

import type { DemandCounters } from '@/api/admin';
import type { UseQueryResult } from '@tanstack/react-query';

/** `[...adminKey(actorId), 'demandCounters', from, to]`: one entry per range (plan §5). */
export function demandCountersKey(actorId: string, from: string, to: string): readonly string[] {
  return [...adminKey(actorId), 'demandCounters', from, to];
}

/**
 * The demand counters for `[from, to]` (ISO dates, from the page's URL). A 4xx (a bad range) is an
 * answer and is not retried; anything else gets one more try, as the other admin reads do.
 */
export function useDemandCounters(
  actorId: string,
  from: string,
  to: string,
): UseQueryResult<DemandCounters> {
  return useQuery({
    queryKey: demandCountersKey(actorId, from, to),
    queryFn: ({ signal }) => fetchDemandCounters(from, to, signal),
    retry: (failureCount, error) => !isClientError(error) && failureCount < 1,
  });
}
