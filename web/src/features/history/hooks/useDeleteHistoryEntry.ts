import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { deleteHistoryEntry } from '@/api/history';

import { deleteHistoryEntryMutationKey, historyRootKey } from './historyKeys';

import type { UseMutationResult } from '@tanstack/react-query';

/**
 * How a delete ended, when it did not throw. A 404 is **not** an error here: the entry is gone,
 * which is what the user asked for — another tab got there first (AC-45). A 409
 * `tailoring_run_in_progress` and a 503 stay errors: the entry was **not** deleted.
 */
export type DeleteHistoryEntryOutcome = 'deleted' | 'already_gone';

/**
 * Delete one history entry (AC-45): `deleteHistoryEntry(id)` under
 * `deleteHistoryEntryMutationKey`, a 404 folded to `'already_gone'`, then the history root
 * invalidated — every page and the workspace's latest-run card.
 *
 * **No optimistic removal** — an irreversible action shows what the server did, not what we hoped
 * (2.2's rule). The invalidation is *returned* from `onSuccess`, so the mutation stays pending until
 * the list has been read again: the row says "Deleting…" until the re-read drops it, and never
 * flickers back to an idle row in between.
 *
 * A caller guards a same-tick double click with `queryClient.isMutating({ mutationKey })` (H-58),
 * never `isPending`, which updates a notify later.
 */
export function useDeleteHistoryEntry(
  userId: string,
): UseMutationResult<DeleteHistoryEntryOutcome, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: deleteHistoryEntryMutationKey,
    mutationFn: async (id: string): Promise<DeleteHistoryEntryOutcome> => {
      try {
        await deleteHistoryEntry(id);
        return 'deleted';
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.status === 404 &&
          error.code === 'tailoring_run_not_found'
        ) {
          return 'already_gone';
        }
        throw error;
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: historyRootKey(userId) }),
  });
}
