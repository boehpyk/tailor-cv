import { useQuery } from '@tanstack/react-query';

import { fetchFitScoreForPair } from '@/api/fitScores';
import { GUEST_SCOPE_MAP } from '@/features/scope/scopeMap';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';
import { isClientError, MAX_TRANSIENT_RETRIES } from '@/features/tailoring/hooks/useTailoringRun';

import type { FitScoreForPair, FitScorePair } from '../types';
import type { ScopeMap } from '@/features/scope/scopeMap';

/**
 * The cache key for one pair's score: `[...keyRoot, 'scoring', 'fitScore', baseCvId, postingId]`.
 * A guest's starts with `'scoring'`, which is why `GUEST_QUERY_ROOTS` lists it: a claim must forget
 * it. An account's sits under `['auth', 'account', userId]`, so a guest and a user never share an
 * entry (AC-44). `null` is the disabled query's key, as in `tailoringRunQueryKey`.
 */
export function fitScoreQueryKey(
  pair: FitScorePair | null,
  map: Pick<ScopeMap, 'keyRoot'> = GUEST_SCOPE_MAP,
): readonly unknown[] {
  return [
    ...map.keyRoot,
    'scoring',
    'fitScore',
    pair?.base_cv_id ?? null,
    pair?.job_posting_id ?? null,
  ];
}

/**
 * The stored score for the pair on screen, or `{ fit_score: null }` when there is none yet.
 *
 * **A read, never a score.** This is what makes a reload show the number already paid for instead
 * of *Check my score* beside a notice that the CV goes to Google, and it is how a `POST` that failed
 * on the wire but finished on the server recovers (AC-46): the mutation's `onError` invalidates this
 * key, and the stored row appears.
 *
 * **No `refetchInterval`.** A score has no lifecycle to watch: it is either stored or not, and the
 * only thing that changes it is this tab's own `POST`, which invalidates. One read per load or
 * invalidation (plan §4).
 *
 * Retries as `useTailoringRun` does: a 4xx is an answer and is not retried; a network failure or a
 * 5xx gets `MAX_TRANSIENT_RETRIES`. A read is safe to retry, which is exactly what a write is not
 * (ADR-0031: reads retry, writes hold).
 *
 * `enabled` is not a type guard and `refetch()` ignores it, so the null branch is real and rejects
 * rather than asserting the pair away.
 */
export function useFitScore(
  pair: FitScorePair | null,
): ReturnType<typeof useQuery<FitScoreForPair>> {
  const map = useScopeMap();
  return useQuery({
    queryKey: fitScoreQueryKey(pair, map),
    queryFn: ({ signal }) => {
      if (pair === null) {
        return Promise.reject(new Error('useFitScore: there is no pair to read.'));
      }
      return fetchFitScoreForPair(map, pair, signal);
    },
    enabled: pair !== null,
    retry: (failureCount, error) => !isClientError(error) && failureCount < MAX_TRANSIENT_RETRIES,
  });
}
