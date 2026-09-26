import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { deleteSavedBaseCv } from '@/api/savedBaseCvs';

import { deleteSavedBaseCvMutationKey, savedBaseCvsQueryKeyPrefix } from './savedCvsKeys';

import type { SavedBaseCvList } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * How a delete ended, when it did not fail. `already_gone` is a 404 `base_cv_not_found`: another tab
 * or a double submit deleted it first. That is the outcome the user asked for, so it resolves rather
 * than rejects — the row goes and a polite note says so, never an error (AC-36).
 */
export type DeleteSavedBaseCvOutcome = 'deleted' | 'already_gone';

/** A 404 on a delete means the thing is not there — which is what the delete was for. */
function isAlreadyGone(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404 && error.code === 'base_cv_not_found';
}

async function deleteOrConfirmGone(id: string): Promise<DeleteSavedBaseCvOutcome> {
  try {
    await deleteSavedBaseCv(id);
    return 'deleted';
  } catch (error) {
    if (isAlreadyGone(error)) {
      return 'already_gone';
    }
    throw error;
  }
}

/**
 * Delete a saved CV by id (`DELETE /api/me/base-cvs/{id}`), then drop that row from every cached
 * list and invalidate them.
 *
 * **No optimistic removal** (AC-36, technical plan §7). The row stays, reading "Deleting…", until
 * the server has answered; a removal shown early and then undone by a 503 would have told the user
 * their CV was gone when it was not. A rejection (503, no answer) means it was **not** deleted.
 *
 * The row is removed from the cache **after** the server said so — that is not optimism, it is the
 * answer — so the list does not depend on the refetch succeeding to stop showing a deleted CV. The
 * invalidation is returned, so pending lasts until the list has been re-read.
 */
export function useDeleteSavedBaseCv(): UseMutationResult<DeleteSavedBaseCvOutcome, Error, string> {
  const queryClient = useQueryClient();

  return useMutation<DeleteSavedBaseCvOutcome, Error, string>({
    mutationKey: deleteSavedBaseCvMutationKey,
    mutationFn: deleteOrConfirmGone,
    onSuccess: (_outcome, id) => {
      queryClient.setQueriesData<SavedBaseCvList>(
        { queryKey: savedBaseCvsQueryKeyPrefix },
        (list) =>
          list === undefined ? undefined : { items: list.items.filter((cv) => cv.id !== id) },
      );
      return queryClient.invalidateQueries({ queryKey: savedBaseCvsQueryKeyPrefix });
    },
  });
}
