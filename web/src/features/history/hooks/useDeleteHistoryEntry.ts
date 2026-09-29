import { useMutation, useQueryClient } from '@tanstack/react-query';

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
 * invalidated. **No optimistic removal** — an irreversible action shows what the server did, not
 * what we hoped (2.2's rule). A caller guards a same-tick double click with
 * `queryClient.isMutating({ mutationKey })` (H-58), never `isPending`.
 *
 * SKELETON (T29): the real key, types and invalidation; the request is not made. T31 implements it.
 */
export function useDeleteHistoryEntry(
  userId: string,
): UseMutationResult<DeleteHistoryEntryOutcome, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: deleteHistoryEntryMutationKey,
    mutationFn: (): Promise<DeleteHistoryEntryOutcome> =>
      Promise.reject(new Error('useDeleteHistoryEntry: not implemented (T31)')),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: historyRootKey(userId) }),
  });
}
