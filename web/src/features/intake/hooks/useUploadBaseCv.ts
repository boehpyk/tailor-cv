import { useMutation, useQueryClient } from '@tanstack/react-query';

import { uploadBaseCv } from '@/api/baseCvs';

import { baseCvsQueryKey } from './useBaseCvs';

import type { BaseCv } from '../types';

/**
 * Upload a base CV and refresh the list.
 *
 * `onSuccess` invalidating `baseCvsQueryKey` is the **only** write path into that cache (CLAUDE.md,
 * technical-plan.md's State section) — nothing here writes the new `BaseCv` into the query cache by
 * hand with `setQueryData`. A hand-written cache write would be a second, easy-to-drift copy of
 * "what does the list look like now"; invalidation instead asks the server the same question the
 * initial load did, so `useBaseCvs` and this mutation can never disagree about the shape of a
 * `BaseCv` — the GET response is the only place that shape is decided.
 *
 * `useMutation` (unlike `useQuery`) has no built-in `AbortSignal`/cancellation plumbing in this
 * TanStack Query version, so `uploadBaseCv`'s optional `signal` parameter is simply unused here —
 * it exists for callers (tests, or a future manual "cancel upload" control) that do have one.
 */
export function useUploadBaseCv(): ReturnType<typeof useMutation<BaseCv, Error, File>> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (file: File) => uploadBaseCv(file),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: baseCvsQueryKey }),
  });
}
