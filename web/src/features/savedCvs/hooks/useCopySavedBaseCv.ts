import { useMutation, useQueryClient } from '@tanstack/react-query';

import { copySavedBaseCv } from '@/api/baseCvs';
import { ApiError } from '@/api/client';
import { baseCvsQueryKey } from '@/features/intake/hooks/useBaseCvs';

import { copySavedBaseCvMutationKey, savedBaseCvsQueryKeyPrefix } from './savedCvsKeys';

import type { BaseCv } from '@/features/intake/types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * "Use this CV": copy a saved CV into this browser's workspace (`POST /api/base-cvs/copies`) by the
 * saved CV's id. On success, invalidate `['intake', 'baseCvs']` — the working copy is then the
 * newest guest CV, which is what `latestBaseCv` already picks, so no new client rule decides that
 * the workspace uses it. The invalidation is returned, so the button stays pending until the
 * workspace has re-read its CVs and shows the copy. On a 404 the saved CV is gone: the saved-CV list
 * is refetched too, so the picker stops offering it (AC-39).
 *
 * Not idempotent — two requests, two copies (S-36) — so the button guards a same-tick double click
 * with `queryClient.isMutating({ mutationKey: copySavedBaseCvMutationKey })`, which `mutate()` bumps
 * synchronously; `isPending` only changes on the next notify (2.1's trap, CLAUDE.md).
 */
export function useCopySavedBaseCv(): UseMutationResult<BaseCv, Error, string> {
  const queryClient = useQueryClient();

  return useMutation<BaseCv, Error, string>({
    mutationKey: copySavedBaseCvMutationKey,
    mutationFn: (savedBaseCvId) => copySavedBaseCv(savedBaseCvId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: baseCvsQueryKey }),
    onError: (error) => {
      if (error instanceof ApiError && error.code === 'base_cv_not_found') {
        void queryClient.invalidateQueries({ queryKey: savedBaseCvsQueryKeyPrefix });
      }
    },
  });
}
