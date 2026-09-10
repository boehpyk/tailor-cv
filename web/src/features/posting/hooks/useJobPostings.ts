import { useQuery } from '@tanstack/react-query';

import { fetchJobPostings } from '@/api/jobPostings';
import { ApiError } from '@/api/client';

import type { JobPostingListResponse } from '../types';

export const jobPostingsQueryKey = ['posting', 'jobPostings'] as const;

/**
 * The guest session's job postings.
 *
 * **The 401 fold.** `GET /api/job-postings` answers 401 `guest_session_expired` for a missing,
 * unknown or expired cookie. This hook folds that one case into the ordinary empty result
 * (`{ items: [] }`) inside `queryFn`, rather than letting it surface as `isError` — because "you
 * have no session" and "you have captured no postings yet" are the **same screen**: the input,
 * ready to use.
 *
 * `useBaseCvs`'s docstring sets out the full reasoning for this choice and where the branch could
 * otherwise live; it is repeated here only in summary, deliberately, so the two hooks do not drift
 * into two different answers for one question.
 *
 * Every other `ApiError` — a genuine 5xx, a network failure, a future different 4xx — is rethrown
 * untouched and lands in the query's normal `isError` branch. Folding those in too would erase the
 * distinction between "nothing here yet" and "this failed", and a user who cannot tell them apart
 * refreshes.
 *
 * **No `useEffect` anywhere in this feature.** There is nothing outside React to synchronize with:
 * the postings are server state and live entirely in TanStack Query.
 */
export function useJobPostings(): ReturnType<typeof useQuery<JobPostingListResponse>> {
  return useQuery({
    queryKey: jobPostingsQueryKey,
    queryFn: async ({ signal }) => {
      try {
        return await fetchJobPostings(signal);
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
