/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the parameters are the signatures qa's T27 tests compile against; T28 uses them and deletes this line. */
import { useMutation } from '@tanstack/react-query';

import type { UseMutationResult } from '@tanstack/react-query';

/**
 * How *Remove from board* ended, when it did not throw. A 404 is **not** an error: the card is gone,
 * which is what the user asked for (AC-37). A 503 stays an error — the card was kept.
 */
export type UntrackOutcome = 'removed' | 'already_gone';

/**
 * *Remove from board* (AC-37), under `untrackApplicationMutationKey(userId)`. Variables: the card id.
 * **No optimistic removal** (2.2's rule for what cannot be taken back): the invalidation of the
 * board is returned from `onSuccess`, so the card reads *"Removing…"* until the re-read drops it.
 * The history root is invalidated too (badges).
 *
 * SKELETON (T26): no key, no request — `mutate()` settles as an error.
 */
export function useUntrackApplication(
  _userId: string,
): UseMutationResult<UntrackOutcome, Error, string> {
  return useMutation({
    mutationFn: (): Promise<UntrackOutcome> =>
      Promise.reject(new Error('useUntrackApplication: not implemented (T26 skeleton)')),
  });
}
