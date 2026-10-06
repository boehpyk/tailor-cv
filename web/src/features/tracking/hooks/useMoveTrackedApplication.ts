import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { moveTrackedApplication } from '@/api/trackedApplications';
import { historyRootKey } from '@/features/history/hooks/historyKeys';

import { mergeApplication, withCard, withoutCard } from './boardCache';
import { boardKey, moveTrackedApplicationMutationKey } from './trackingKeys';

import type { Board, BoardCard, Stage, TrackedApplication } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/** One move: the card, the stage it goes to, and the `version` the board showed — the pre-move one. */
export interface MoveVariables {
  readonly id: string;
  readonly stage: Stage;
  readonly version: number;
}

/** What `onMutate` hands `onError`: the board as it was, restored on a refusal. */
export interface MoveContext {
  readonly previous: Board | undefined;
}

/** Which card a refusal was about, and whether it is back on the board. */
export interface RefusedCard {
  readonly id: string;
  readonly restored: boolean;
}

/**
 * What the page hears about **every** move, not only the latest one.
 *
 * Why not `mutate(vars, { onSuccess })` or `mutation.error`? Both follow the observer's *latest*
 * mutation, and moves on two cards can overlap (T-39: "nothing else blocked"). Card A's refusal
 * landing after card B was moved would then roll A back with no word said — the silent snap-back
 * AC-34 forbids. These run from the mutation's own callbacks, once per mutation.
 */
export interface MoveEvents {
  /** The server accepted: `card` as it was before the move, `stage` where it went. */
  readonly onMoved?: (card: BoardCard, stage: Stage) => void;
  /**
   * The server (or the network) refused; the card has already been put back (`restored: true`) or
   * removed (a 404, `restored: false`).
   */
  readonly onRefused?: (error: Error, card: RefusedCard) => void;
}

function isGone(error: Error): boolean {
  return error instanceof ApiError && error.code === 'tracked_application_not_found';
}

/**
 * **The optimistic mutation** (plan §7, AC-33, AC-34) — by control and by drag alike, under
 * `moveTrackedApplicationMutationKey(userId)`:
 *
 * 1. `onMutate`: cancel the board query (T-41) so a read already on the wire cannot land on top of
 *    the move, snapshot the entry, write the card's new stage and `stage_changed_at` into the
 *    **same** entry, re-sorted; return `{ previous }`.
 * 2. `mutationFn`: `PUT …/stage` with the pre-move `version`.
 * 3. `onError`: a 404 removes the card (it is gone, T-22); anything else puts **this card** back as
 *    the snapshot had it. Only this card: restoring the whole snapshot would also undo another
 *    card's move that started after it and is still in flight.
 * 4. `onSuccess`: write the server's card (its new `version`) into the entry, so the next move sends
 *    the version the server now holds.
 * 5. `onSettled`: invalidate the board — **unless another move is still in flight**, whose
 *    optimistic state a refetch would overwrite with a server that has not seen it yet; the last move
 *    to settle refetches for all of them — and always the history pages' root (badges, AC-39).
 *    Neither is awaited: a refetch that hangs must not hold the refusal's copy back.
 *
 * The cache *is* the optimistic state — never a parallel `useState` copy. A caller guards a
 * same-tick double choice with `queryClient.isMutating({ mutationKey, predicate })`, never
 * `isPending`, which updates a notify later (T-29).
 */
export function useMoveTrackedApplication(
  userId: string,
  events: MoveEvents = {},
): UseMutationResult<TrackedApplication, Error, MoveVariables, MoveContext> {
  const queryClient = useQueryClient();
  const key = boardKey(userId);
  const mutationKey = moveTrackedApplicationMutationKey(userId);

  return useMutation<TrackedApplication, Error, MoveVariables, MoveContext>({
    mutationKey,
    mutationFn: ({ id, stage, version }) => moveTrackedApplication(id, stage, version),
    onMutate: async ({ id, stage }) => {
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<Board>(key);
      const movedAt = new Date().toISOString();
      queryClient.setQueryData<Board>(key, (board) =>
        board === undefined
          ? board
          : withCard(board, id, (card) => ({ ...card, stage, stage_changed_at: movedAt })),
      );
      return { previous };
    },
    onError: (error, { id }, context) => {
      let restored = false;
      if (isGone(error)) {
        queryClient.setQueryData<Board>(key, (board) =>
          board === undefined ? board : withoutCard(board, id),
        );
      } else {
        const before = context?.previous?.items.find((card) => card.id === id);
        if (before !== undefined) {
          queryClient.setQueryData<Board>(key, (board) =>
            board === undefined ? board : withCard(board, id, () => before),
          );
          restored = true;
        }
      }
      events.onRefused?.(error, { id, restored });
    },
    onSuccess: (application, { id, stage }, context) => {
      const before = context.previous?.items.find((card) => card.id === id);
      queryClient.setQueryData<Board>(key, (board) =>
        board === undefined
          ? board
          : withCard(board, id, (card) => mergeApplication(card, application)),
      );
      if (before !== undefined) {
        events.onMoved?.(before, stage);
      }
    },
    onSettled: () => {
      // This mutation still counts as pending while its own `onSettled` runs, hence `=== 1`.
      if (queryClient.isMutating({ mutationKey }) === 1) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
      void queryClient.invalidateQueries({ queryKey: historyRootKey(userId) });
    },
  });
}
