import { useMutation, useQueryClient } from '@tanstack/react-query';

import { requestFitScore } from '@/api/fitScores';
import { GUEST_SCOPE_MAP } from '@/features/scope/scopeMap';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';

import { fitScoreQueryKey } from './useFitScore';

import type { FitScore, FitScorePair } from '../types';
import type { ScopeMap } from '@/features/scope/scopeMap';

/**
 * The score request's mutation key in a scope. The caller guards a same-tick double click with
 * `queryClient.isMutating({ mutationKey })`, which `mutate()` updates synchronously; `isPending`
 * updates a tick later, so two clicks in one tick would both read `false` and pay twice (AC-48).
 */
export function requestFitScoreMutationKey(
  map: Pick<ScopeMap, 'keyRoot'> = GUEST_SCOPE_MAP,
): readonly unknown[] {
  return [...map.keyRoot, 'scoring', 'requestFitScore'];
}

/**
 * Score a pair: **the paid call**, made on a click and nowhere else.
 *
 * - **No `retry`.** A retried `POST` that timed out on our side but finished on the server is a
 *   second Gemini call (`retry.grep.test.ts`). A failure is shown, and the person decides.
 * - **`onSuccess` returns the invalidation**, as `useCreateTailoringRun` does: the mutation stays
 *   pending until the pair's `GET` has re-read the row, so the panel goes straight from *Checking…*
 *   to the score, with no frame of an empty *Check my score* between them. Nothing is written with
 *   `setQueryData`: the `GET`'s answer is the one source.
 * - **`onError` invalidates without returning** (AC-46): the failure shows at once, and if the
 *   server finished anyway (a dropped connection, S-17) the re-read finds the stored row and the
 *   score replaces the failure.
 *
 * Both callbacks are the hook's, not `mutate()`'s, so they still run when the panel unmounts
 * mid-call: the score lands in the cache for the next visit.
 */
export function useRequestFitScore(): ReturnType<
  typeof useMutation<FitScore, Error, FitScorePair>
> {
  const queryClient = useQueryClient();
  const map = useScopeMap();

  return useMutation({
    mutationKey: requestFitScoreMutationKey(map),
    mutationFn: (pair: FitScorePair) => requestFitScore(map, pair),
    onSuccess: (_score, pair) =>
      queryClient.invalidateQueries({ queryKey: fitScoreQueryKey(pair, map) }),
    onError: (_error, pair) => {
      void queryClient.invalidateQueries({ queryKey: fitScoreQueryKey(pair, map) });
    },
  });
}
