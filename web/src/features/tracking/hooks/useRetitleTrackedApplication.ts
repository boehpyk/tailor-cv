/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the parameters are the signatures qa's T27 tests compile against; T28 uses them and deletes this line. */
import { useMutation } from '@tanstack/react-query';

import type { TrackedApplication } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/** One retitle: the card, the title as typed (`null` clears it), and the `version` the board showed. */
export interface RetitleVariables {
  readonly id: string;
  readonly title: string | null;
  readonly version: number;
}

/**
 * Set or clear a card's title (AC-36), under `retitleTrackedApplicationMutationKey(userId)`.
 * **Not optimistic**: the server normalizes the title, so the card shows what it stored. On success
 * the server's card is written into the board and the board and history root invalidated; a 409
 * refetches the board.
 *
 * SKELETON (T26): no key, no request — `mutate()` settles as an error.
 */
export function useRetitleTrackedApplication(
  _userId: string,
): UseMutationResult<TrackedApplication, Error, RetitleVariables> {
  return useMutation<TrackedApplication, Error, RetitleVariables>({
    mutationFn: (): Promise<TrackedApplication> =>
      Promise.reject(new Error('useRetitleTrackedApplication: not implemented (T26 skeleton)')),
  });
}
