import { useMutation } from '@tanstack/react-query';

import { deleteSavedBaseCvMutationKey } from './savedCvsKeys';

import type { UseMutationResult } from '@tanstack/react-query';

/**
 * How a delete ended, when it did not fail. `already_gone` is a 404 `base_cv_not_found`: another tab
 * or a double submit deleted it first. That is the outcome the user asked for, so it resolves rather
 * than rejects — the row goes and a polite note says so, never an error (AC-36).
 */
export type DeleteSavedBaseCvOutcome = 'deleted' | 'already_gone';

/**
 * Delete a saved CV by id (`DELETE /api/me/base-cvs/{id}`), then invalidate the saved-CV lists.
 *
 * **No optimistic removal** (AC-36, technical plan §7). The row stays, reading "Deleting…", until
 * the server has answered; a removal shown early and then undone by a 503 would have told the user
 * their CV was gone when it was not. A rejection (503, no answer) means it was **not** deleted.
 *
 * SKELETON (T25): the real key; the mutation rejects without a request. GREEN is T27.
 */
export function useDeleteSavedBaseCv(): UseMutationResult<DeleteSavedBaseCvOutcome, Error, string> {
  return useMutation<DeleteSavedBaseCvOutcome, Error, string>({
    mutationKey: deleteSavedBaseCvMutationKey,
    mutationFn: () => Promise.reject(new Error('useDeleteSavedBaseCv: not implemented (T27)')),
  });
}
