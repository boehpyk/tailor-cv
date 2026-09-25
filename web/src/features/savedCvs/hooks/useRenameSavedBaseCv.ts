import { useMutation } from '@tanstack/react-query';

import { renameSavedBaseCvMutationKey } from './savedCvsKeys';

import type { SavedBaseCv } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/** What a rename sends: which CV, and its new label — `null` clears it (the filename shows again). */
export interface RenameSavedBaseCvVariables {
  readonly id: string;
  readonly label: string | null;
}

/**
 * Rename a saved CV (`PATCH /api/me/base-cvs/{id}`). On success the response — the server's view of
 * the renamed CV — replaces that row in the current user's list (`setQueryData`), so the new name
 * shows without a round trip, **and** the list is invalidated, so the next read is the server's
 * again (AC-37). The label is sent as typed; `invalid_label` is the server's to decide.
 *
 * SKELETON (T25): the real key; the mutation rejects without a request. GREEN is T27.
 */
export function useRenameSavedBaseCv(): UseMutationResult<
  SavedBaseCv,
  Error,
  RenameSavedBaseCvVariables
> {
  return useMutation<SavedBaseCv, Error, RenameSavedBaseCvVariables>({
    mutationKey: renameSavedBaseCvMutationKey,
    mutationFn: () => Promise.reject(new Error('useRenameSavedBaseCv: not implemented (T27)')),
  });
}
