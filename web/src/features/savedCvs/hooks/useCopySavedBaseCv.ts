import { useMutation } from '@tanstack/react-query';

import { copySavedBaseCvMutationKey } from './savedCvsKeys';

import type { BaseCv } from '@/features/intake/types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * "Use this CV": copy a saved CV into this browser's workspace (`POST /api/base-cvs/copies`) by the
 * saved CV's id. On success, invalidate `['intake', 'baseCvs']` — the working copy is then the
 * newest guest CV, which is what `latestBaseCv` already picks, so no new client rule decides that
 * the workspace uses it. On a 404 the saved CV is gone: the saved-CV list is refetched too (AC-39).
 *
 * Not idempotent — two requests, two copies (S-36) — so the button guards a same-tick double click
 * with `queryClient.isMutating({ mutationKey: copySavedBaseCvMutationKey })`, which `mutate()` bumps
 * synchronously; `isPending` only changes on the next notify (2.1's trap, CLAUDE.md).
 *
 * SKELETON (T25): the real key; the mutation rejects without a request. GREEN is T27.
 */
export function useCopySavedBaseCv(): UseMutationResult<BaseCv, Error, string> {
  return useMutation<BaseCv, Error, string>({
    mutationKey: copySavedBaseCvMutationKey,
    mutationFn: () => Promise.reject(new Error('useCopySavedBaseCv: not implemented (T27)')),
  });
}
