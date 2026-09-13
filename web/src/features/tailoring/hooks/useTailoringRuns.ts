import { useQuery } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { fetchTailoringRuns } from '@/api/tailoringRuns';

import type { TailoringRunListResponse } from '../types';

export const tailoringRunsQueryKey = ['tailoring', 'tailoringRuns'] as const;

/**
 * The guest session's tailoring runs, newest first, as summaries without the documents.
 *
 * **The 401 fold.** `GET /api/tailoring-runs` answers 401 `guest_session_expired` for a missing,
 * unknown or expired cookie. This hook folds that one case into the ordinary empty result
 * (`{ items: [] }`) inside `queryFn`, rather than letting it surface as `isError` — because "you have
 * no session" and "you have not tailored anything yet" are the **same screen**: the launch button,
 * ready to use once both inputs exist.
 *
 * `useBaseCvs`'s docstring sets out the full reasoning for this choice and where the branch could
 * otherwise live; `useJobPostings` repeats it in summary and so does this hook, deliberately, so the
 * three do not drift into three different answers for one question.
 *
 * Every other `ApiError` — a genuine 5xx, a network failure, a future different 4xx — is rethrown
 * untouched and lands in the query's normal `isError` branch. Folding those in too would erase the
 * distinction between "nothing here yet" and "this failed", and on this feature a user who cannot
 * tell them apart does not just refresh: they pay for a second LLM call.
 *
 * The list is also what makes a refresh **reattach** to a run in progress rather than start a new
 * one: its newest item is the run to watch on load.
 */
export function useTailoringRuns(): ReturnType<typeof useQuery<TailoringRunListResponse>> {
  return useQuery({
    queryKey: tailoringRunsQueryKey,
    queryFn: async ({ signal }) => {
      try {
        return await fetchTailoringRuns(signal);
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.status === 401 &&
          error.code === 'guest_session_expired'
        ) {
          return { items: [] };
        }
        throw error;
      }
    },
  });
}
