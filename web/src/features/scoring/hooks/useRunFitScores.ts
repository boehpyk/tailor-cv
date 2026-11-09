import { useQuery } from '@tanstack/react-query';

import { fetchRunFitScores } from '@/api/tailoringRuns';
import { GUEST_SCOPE_MAP } from '@/features/scope/scopeMap';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';
import { isClientError, MAX_TRANSIENT_RETRIES } from '@/features/tailoring/hooks/useTailoringRun';

import type { RunFitScores } from '../types';
import type { ScopeMap } from '@/features/scope/scopeMap';
import type { UseQueryResult } from '@tanstack/react-query';

/** `[...keyRoot, 'scoring', 'runFitScores', runId]` (plan §5). The route is an account's only. */
export function runFitScoresQueryKey(
  runId: string,
  map: Pick<ScopeMap, 'keyRoot'> = GUEST_SCOPE_MAP,
): readonly unknown[] {
  return [...map.keyRoot, 'scoring', 'runFitScores', runId];
}

/**
 * A run's stored before and after scores and the server's verdict (slice 5.2). **A read, never a
 * score**: this is what makes a reload show a comparison already paid for, and how a `POST` that
 * failed on the wire but finished on the server recovers (`useCompareScores` invalidates it).
 * Reads retry (ADR-0031). The verdict is the domain's; the client never computes one.
 */
export function useRunFitScores(runId: string): UseQueryResult<RunFitScores> {
  const map = useScopeMap();
  return useQuery({
    queryKey: runFitScoresQueryKey(runId, map),
    queryFn: ({ signal }) => fetchRunFitScores(runId, signal),
    retry: (failureCount, error) => !isClientError(error) && failureCount < MAX_TRANSIENT_RETRIES,
  });
}
