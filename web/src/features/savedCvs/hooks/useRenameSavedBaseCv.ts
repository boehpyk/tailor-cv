import { useMutation, useQueryClient } from '@tanstack/react-query';

import { renameSavedBaseCv } from '@/api/savedBaseCvs';

import { renameSavedBaseCvMutationKey, savedBaseCvsQueryKeyPrefix } from './savedCvsKeys';

import type { SavedBaseCv, SavedBaseCvList } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/** What a rename sends: which CV, and its new label — `null` clears it (the filename shows again). */
export interface RenameSavedBaseCvVariables {
  readonly id: string;
  readonly label: string | null;
}

/**
 * Rename a saved CV (`PATCH /api/me/base-cvs/{id}`). On success the response — the server's view of
 * the renamed CV — replaces that row in every cached list (`setQueriesData`), so the new name shows
 * without a round trip, **and** the lists are invalidated, so the next read is the server's again
 * (AC-37). The label is sent as typed; `invalid_label` is the server's to decide.
 *
 * Writing the response into the cache is not the "copy server data by hand" mistake: the response
 * *is* server data, it replaces exactly the row it describes, and the invalidation means the cache
 * is re-read from the server rather than trusted.
 */
export function useRenameSavedBaseCv(): UseMutationResult<
  SavedBaseCv,
  Error,
  RenameSavedBaseCvVariables
> {
  const queryClient = useQueryClient();

  return useMutation<SavedBaseCv, Error, RenameSavedBaseCvVariables>({
    mutationKey: renameSavedBaseCvMutationKey,
    mutationFn: ({ id, label }) => renameSavedBaseCv(id, label),
    onSuccess: (renamed) => {
      queryClient.setQueriesData<SavedBaseCvList>(
        { queryKey: savedBaseCvsQueryKeyPrefix },
        (list) =>
          list === undefined
            ? undefined
            : { items: list.items.map((cv) => (cv.id === renamed.id ? renamed : cv)) },
      );
      void queryClient.invalidateQueries({ queryKey: savedBaseCvsQueryKeyPrefix });
    },
  });
}
