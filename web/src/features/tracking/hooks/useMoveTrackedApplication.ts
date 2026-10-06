/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the parameters are the signatures qa's T27 tests compile against; T28 uses them and deletes this line. */
import { useMutation } from '@tanstack/react-query';

import type { Board, Stage, TrackedApplication } from '../types';
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

/**
 * **The optimistic mutation** (plan §7, AC-33, AC-34) — by control and by drag alike, under
 * `moveTrackedApplicationMutationKey(userId)`:
 *
 * 1. `onMutate`: cancel the board query (T-41), snapshot it, write the card's new stage and
 *    `stage_changed_at` into the **same** cache entry, re-sorted; return `{ previous }`.
 * 2. `mutationFn`: `PUT …/stage` with the pre-move `version`.
 * 3. `onError`: restore the snapshot; a 404 also removes the card; a 409 refetches.
 * 4. `onSuccess`: write the server's card (its new `version`) into the entry.
 * 5. `onSettled`: invalidate the board and the history pages' root (badges).
 *
 * The cache *is* the optimistic state — never a parallel `useState` copy. The failure's copy is
 * `moveFailureCopy(mutation.error)`.
 *
 * SKELETON (T26): no key, no cache work, no request — `mutate()` settles as an error.
 */
export function useMoveTrackedApplication(
  _userId: string,
): UseMutationResult<TrackedApplication, Error, MoveVariables, MoveContext> {
  return useMutation<TrackedApplication, Error, MoveVariables, MoveContext>({
    mutationFn: (): Promise<TrackedApplication> =>
      Promise.reject(new Error('useMoveTrackedApplication: not implemented (T26 skeleton)')),
  });
}
