import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { retitleTrackedApplication } from '@/api/trackedApplications';
import { historyRootKey } from '@/features/history/hooks/historyKeys';

import { mergeApplication, withCard } from './boardCache';
import { boardKey, retitleTrackedApplicationMutationKey } from './trackingKeys';

import type { Board, TrackedApplication } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/** One retitle: the card, the title as typed (`null` clears it), and the `version` the board showed. */
export interface RetitleVariables {
  readonly id: string;
  readonly title: string | null;
  readonly version: number;
}

/**
 * Set or clear a card's title (AC-36), under `retitleTrackedApplicationMutationKey(userId)`.
 * **Not optimistic**: the server normalizes the title (trims it), so the card shows what it stored,
 * not what was typed. On success the server's card is written into the board and the board and the
 * history root are invalidated; a 409 or a 404 re-reads the board — it holds a newer version, or no
 * card at all.
 *
 * Each `CardTitleEditor` owns its own observer of this mutation, so "which card is saving" and
 * "why that one failed" are per card without any bookkeeping here.
 */
export function useRetitleTrackedApplication(
  userId: string,
): UseMutationResult<TrackedApplication, Error, RetitleVariables> {
  const queryClient = useQueryClient();
  const key = boardKey(userId);
  return useMutation<TrackedApplication, Error, RetitleVariables>({
    mutationKey: retitleTrackedApplicationMutationKey(userId),
    mutationFn: ({ id, title, version }) => retitleTrackedApplication(id, title, version),
    onSuccess: (application, { id }) => {
      queryClient.setQueryData<Board>(key, (board) =>
        board === undefined
          ? board
          : withCard(board, id, (card) => mergeApplication(card, application)),
      );
      void queryClient.invalidateQueries({ queryKey: key });
      void queryClient.invalidateQueries({ queryKey: historyRootKey(userId) });
    },
    onError: (error) => {
      if (
        error instanceof ApiError &&
        (error.code === 'tracked_application_version_conflict' ||
          error.code === 'tracked_application_not_found')
      ) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
    },
  });
}
