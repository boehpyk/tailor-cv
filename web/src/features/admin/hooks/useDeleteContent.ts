import { useMutation, useQueryClient } from '@tanstack/react-query';

import { deleteUserCv, deleteUserHistoryEntry } from '@/api/admin';
import { ApiError } from '@/api/client';

import { adminContentKey } from './useAccountContent';
import { adminUserKey } from './useAdminUser';

import type { AdminBaseCvDeletion, AdminHistoryEntryErasure } from '@/api/admin';
import type { UseMutationResult } from '@tanstack/react-query';

/** The two things an admin deletes from an account (AC-29). */
export type ContentKind = 'cv' | 'history';

export type ContentDeletion =
  | { readonly kind: 'cv'; readonly outcome: 'deleted'; readonly report: AdminBaseCvDeletion }
  | {
      readonly kind: 'history';
      readonly outcome: 'deleted';
      readonly report: AdminHistoryEntryErasure;
    }
  /** The kind's own 404: a second tab, the owner, or a double submit got there first. */
  | { readonly kind: ContentKind; readonly outcome: 'already_gone' };

/** One key for both deletes: a caller guards a same-tick double click with `isMutating` (2.1). */
export const deleteContentMutationKey = ['auth', 'account', 'admin', 'deleteContent'] as const;

const NOT_FOUND_CODE: Readonly<Record<ContentKind, string>> = {
  cv: 'base_cv_not_found',
  history: 'tailoring_run_not_found',
};

/** The item's cache key; a run's exports sit under it. */
const ITEM_PART: Readonly<Record<ContentKind, string>> = { cv: 'cv', history: 'run' };

async function remove(userId: string, kind: ContentKind, id: string): Promise<ContentDeletion> {
  try {
    return kind === 'cv'
      ? { kind, outcome: 'deleted', report: await deleteUserCv(userId, id) }
      : { kind, outcome: 'deleted', report: await deleteUserHistoryEntry(userId, id) };
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && error.code === NOT_FOUND_CODE[kind]) {
      return { kind, outcome: 'already_gone' };
    }
    throw error;
  }
}

export interface UseDeleteContentOptions {
  /**
   * Runs, awaited, after a delete (or an *already gone*) and **before** the cache is touched —
   * where the page navigates back to the account with its notice. From the mutation's own
   * `onSuccess`, because TanStack skips a mutate-level callback on an unmounted observer (2.2).
   */
  readonly onDeleted?: (deletion: ContentDeletion) => Promise<void> | void;
}

/**
 * Delete one saved CV or one history entry of `userId` (AC-29). **Not optimistic**, never re-sent
 * (ADR-0031). A 409 `tailoring_run_in_progress`, `user_not_found` or a 503 is the mutation's
 * `error` and changes nothing locally.
 *
 * On success (or the kind's 404): `onDeleted`, then the item's key is removed (with a run's
 * exports) and everything under the account's detail key is invalidated — the lists and the
 * overview's counts.
 */
export function useDeleteContent(
  actorId: string,
  userId: string,
  kind: ContentKind,
  id: string,
  { onDeleted }: UseDeleteContentOptions = {},
): UseMutationResult<ContentDeletion, Error, void> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: deleteContentMutationKey,
    mutationFn: () => remove(userId, kind, id),
    onSuccess: async (deletion) => {
      await onDeleted?.(deletion);
      queryClient.removeQueries({
        queryKey: adminContentKey(actorId, userId, ITEM_PART[kind], id),
      });
      await queryClient.invalidateQueries({ queryKey: adminUserKey(actorId, userId) });
    },
  });
}
