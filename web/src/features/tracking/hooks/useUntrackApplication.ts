import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { untrackApplication } from '@/api/trackedApplications';
import { historyRootKey } from '@/features/history/hooks/historyKeys';

import { invalidateBoardUnlessMoving, withoutCard } from './boardCache';
import { boardKey, untrackApplicationMutationKey } from './trackingKeys';

import type { Board } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * How *Remove from board* ended, when it did not throw. A 404 is **not** an error: the card is gone,
 * which is what the user asked for (AC-37). A 503 stays an error — the card was kept.
 */
export type UntrackOutcome = 'removed' | 'already_gone';

/** What the page hears about every removal (see `MoveEvents` for why not `mutate`'s callbacks). */
export interface UntrackEvents {
  readonly onRemoved?: (outcome: UntrackOutcome) => void;
}

/**
 * *Remove from board* (AC-37), under `untrackApplicationMutationKey(userId)`. Variables: the card id.
 *
 * **No optimistic removal** (2.2's rule for what cannot be taken back): the card reads
 * *"Removing…"* until the server has answered. Then — and only then — the card is dropped from the
 * cache entry (the server said it is gone, so this is a fact, not a hope; a failed re-read must not
 * bring it back), and the board and the history root (badges) are invalidated — the board only when
 * no move is in flight (`invalidateBoardUnlessMoving`); otherwise the last move's settle re-reads it.
 */
export function useUntrackApplication(
  userId: string,
  events: UntrackEvents = {},
): UseMutationResult<UntrackOutcome, Error, string> {
  const queryClient = useQueryClient();
  const key = boardKey(userId);
  return useMutation({
    mutationKey: untrackApplicationMutationKey(userId),
    mutationFn: async (id: string): Promise<UntrackOutcome> => {
      try {
        await untrackApplication(id);
        return 'removed';
      } catch (error) {
        if (error instanceof ApiError && error.code === 'tracked_application_not_found') {
          return 'already_gone';
        }
        throw error;
      }
    },
    onSuccess: (outcome, id) => {
      queryClient.setQueryData<Board>(key, (board) =>
        board === undefined ? board : withoutCard(board, id),
      );
      invalidateBoardUnlessMoving(queryClient, userId);
      void queryClient.invalidateQueries({ queryKey: historyRootKey(userId) });
      events.onRemoved?.(outcome);
    },
  });
}
