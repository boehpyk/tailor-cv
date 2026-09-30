import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { deleteHistoryEntry } from '@/api/history';
import { exportJobsQueryKey } from '@/features/export/hooks/useExportJobs';
import { jobPostingsKey } from '@/features/posting/hooks/useJobPostings';
import { scopeMap } from '@/features/scope/scopeMap';
import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';

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
 * `deleteHistoryEntryMutationKey`, a 404 folded to `'already_gone'`, then the cache brought in line
 * with what the server now holds:
 *
 * - the run's own query and its export jobs are **removed**, not invalidated. An invalidated entry
 *   keeps its data, so an observer that remounts before the refetch (the back button landing on
 *   `/history/{id}/cv`) would render a document that no longer exists anywhere;
 * - the account's job postings are **invalidated**. The deletion may have taken the posting with it
 *   (a posting survives only while some run still references it), and the server alone knows
 *   whether it did, so the picker re-reads rather than guessing;
 * - the history root is invalidated: every page and the workspace's latest-run card.
 *
 * Every key comes from the builder its reader uses, parameterized by the account `ScopeMap`. A key
 * written out here by hand would be a second spelling that can drift from the first, and a removal
 * aimed at a key nobody reads does nothing and says nothing.
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
    onSuccess: (_outcome, id) => {
      const map = scopeMap({ kind: 'account', userId });
      queryClient.removeQueries({ queryKey: tailoringRunQueryKey(id, map), exact: true });
      queryClient.removeQueries({ queryKey: exportJobsQueryKey(id, map), exact: true });
      return Promise.all([
        queryClient.invalidateQueries({ queryKey: jobPostingsKey(map) }),
        queryClient.invalidateQueries({ queryKey: historyRootKey(userId) }),
      ]);
    },
  });
}
