import { useMutation, useQueryClient } from '@tanstack/react-query';

import { createJobPosting } from '@/api/jobPostings';

import { jobPostingsQueryKey } from './useJobPostings';

import type { JobPosting, NewJobPosting } from '../types';

/**
 * Capture a job posting and refresh the list.
 *
 * `onSuccess` invalidating `jobPostingsQueryKey` is the **only** write path into that cache — no
 * `setQueryData` anywhere. A hand-written cache write would be a second, easy-to-drift copy of
 * "what does the list look like now", and it would drift in a specific way here: the POST returns
 * a full `JobPosting` (with `text`) while the list holds `JobPostingSummary` (with `preview`), so
 * writing the response straight into the list cache would put an object of the wrong shape in it.
 * Invalidating asks the server the same question the initial load did, so the two can never
 * disagree about what a list item is.
 *
 * `useMutation` has no built-in `AbortSignal` plumbing in this TanStack Query version, so
 * `createJobPosting`'s optional `signal` is unused here — it exists for callers that have one.
 */
export function useCreateJobPosting(): ReturnType<
  typeof useMutation<JobPosting, Error, NewJobPosting>
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (input: NewJobPosting) => createJobPosting(input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: jobPostingsQueryKey }),
  });
}
