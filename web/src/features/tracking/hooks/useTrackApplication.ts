/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the parameters are the signatures qa's T27 tests compile against; T28 uses them and deletes this line. */
import { useMutation } from '@tanstack/react-query';

import type { TrackedApplication } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * How *Add to board* ended, when it did not throw. A 409 `application_already_tracked` is **not** an
 * error here — the run is on the board, which is what the user asked for (another tab, a double
 * submit); its details name the existing card.
 */
export type TrackOutcome =
  | { readonly kind: 'tracked'; readonly application: TrackedApplication }
  | { readonly kind: 'already_tracked'; readonly trackedApplicationId: string };

/**
 * *Add to board* (AC-38): `trackApplication({ tailoring_run_id })` under
 * `trackApplicationMutationKey(userId)`; on success the board and the history pages' root are
 * invalidated (badges, AC-39). Variables: the run id. Not optimistic.
 *
 * A caller guards a same-tick double click with `queryClient.isMutating({ mutationKey })`.
 *
 * SKELETON (T26): no mutation key, no request, no cache work — `mutate()` settles as an error.
 */
export function useTrackApplication(
  _userId: string,
): UseMutationResult<TrackOutcome, Error, string> {
  return useMutation({
    mutationFn: (): Promise<TrackOutcome> =>
      Promise.reject(new Error('useTrackApplication: not implemented (T26 skeleton)')),
  });
}
