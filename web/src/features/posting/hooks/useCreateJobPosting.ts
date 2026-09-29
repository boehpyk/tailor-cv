import { useMutation, useQueryClient } from '@tanstack/react-query';

import { createJobPosting } from '@/api/jobPostings';
import { GUEST_SCOPE_MAP } from '@/features/scope/scopeMap';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';

import { jobPostingsKey } from './useJobPostings';

import type { JobPosting, NewJobPosting } from '../types';
import type { ScopeMap } from '@/features/scope/scopeMap';

/**
 * The mutation's key — observable from outside the panel through `useIsMutating({ mutationKey })`,
 * for the reason `uploadBaseCvMutationKey` gives: the workspace's stepper marks stage 2 active
 * while a paste or a fetch is on the wire, and the panel keeps its mutation to itself.
 */
export const createJobPostingMutationKey = ['posting', 'createJobPosting'] as const;

/**
 * The mutation key in a scope — `createJobPostingMutationKey` itself for a guest, rooted under the
 * account for an account (slice 2.3), so a workspace's `useIsMutating` counts its own scope's
 * submissions and no other's.
 */
export function createJobPostingMutationKeyFor(
  map: Pick<ScopeMap, 'keyRoot'> = GUEST_SCOPE_MAP,
): readonly unknown[] {
  return [...map.keyRoot, ...createJobPostingMutationKey];
}

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
  const map = useScopeMap();

  return useMutation({
    mutationKey: createJobPostingMutationKeyFor(map),
    mutationFn: (input: NewJobPosting) => createJobPosting(map, input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: jobPostingsKey(map) }),
  });
}
