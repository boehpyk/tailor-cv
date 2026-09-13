import { useMutation, useQueryClient } from '@tanstack/react-query';

import { createTailoringRun } from '@/api/tailoringRuns';

import { tailoringRunsQueryKey } from './useTailoringRuns';

import type { NewTailoringRun, TailoringRun } from '../types';

/**
 * Request a tailoring run and refresh the list.
 *
 * `onSuccess` invalidating `tailoringRunsQueryKey` is the **only** write path into that cache — no
 * `setQueryData` anywhere, for the reason `useCreateJobPosting`'s docstring gives, which applies here
 * with more force: the POST returns a full `TailoringRun` (with `tailored_cv` and `cover_letter`
 * fields) while the list holds `TailoringRunSummary` (without them), so writing the response into the
 * list cache would put an object of the wrong shape there — and the extra fields it carries are
 * exactly the ones the list omits for privacy. Invalidating asks the server the same question the
 * initial load did, so the two can never disagree about what a list item is.
 *
 * Nothing seeds the single-run cache either. The caller takes the returned `id` and hands it to
 * `useTailoringRun`, which fetches it; that costs one extra request and keeps one source of truth.
 * The invalidation does not touch that cache by accident: TanStack matches keys by prefix, and
 * `['tailoring', 'tailoringRuns']` is not a prefix of `['tailoring', 'tailoringRun', id]`.
 *
 * A rejection (401, 404, 409, 413, 422, 429, 503) surfaces as the mutation's `error` — an `ApiError`
 * the component branches on by `code` — and invalidates nothing, because nothing was created.
 *
 * `useMutation` has no built-in `AbortSignal` plumbing in this TanStack Query version, so
 * `createTailoringRun`'s optional `signal` is unused here — it exists for callers that have one.
 */
export function useCreateTailoringRun(): ReturnType<
  typeof useMutation<TailoringRun, Error, NewTailoringRun>
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (input: NewTailoringRun) => createTailoringRun(input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: tailoringRunsQueryKey }),
  });
}
