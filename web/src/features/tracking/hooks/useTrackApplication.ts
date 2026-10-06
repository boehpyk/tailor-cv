import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { trackApplication } from '@/api/trackedApplications';
import { historyRootKey } from '@/features/history/hooks/historyKeys';

import { boardKey, trackApplicationMutationKey } from './trackingKeys';

import type { TrackedApplication } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * How *Add to board* ended, when it did not throw. A 409 `application_already_tracked` is **not** an
 * error here — the run is on the board, which is what the user asked for (another tab, a double
 * submit); its details name the existing card.
 */
export type TrackOutcome =
  | { readonly kind: 'tracked'; readonly application: TrackedApplication }
  | { readonly kind: 'already_tracked'; readonly trackedApplicationId: string | null };

/**
 * *Add to board* (AC-38): `trackApplication({ tailoring_run_id })` under
 * `trackApplicationMutationKey(userId)`; on success the board and the history pages' root are
 * invalidated (badges, AC-39). Variables: the run id. Not optimistic.
 *
 * The board's invalidation is **returned** from `onSuccess`, so the button reads *"Adding…"* until
 * the re-read board holds the card — then the badge reads its stage from the board, with no
 * in-between flash of *Add to board*. That is also how an `already_tracked` learns *which* stage
 * the card is in: this tab never saw it.
 *
 * A caller guards a same-tick double click with `queryClient.isMutating({ mutationKey, predicate })`.
 */
export function useTrackApplication(
  userId: string,
): UseMutationResult<TrackOutcome, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: trackApplicationMutationKey(userId),
    mutationFn: async (runId: string): Promise<TrackOutcome> => {
      try {
        const application = await trackApplication({ tailoring_run_id: runId });
        return { kind: 'tracked', application };
      } catch (error) {
        if (error instanceof ApiError && error.code === 'application_already_tracked') {
          const id = error.details.tracked_application_id;
          return {
            kind: 'already_tracked',
            trackedApplicationId: typeof id === 'string' ? id : null,
          };
        }
        throw error;
      }
    },
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: boardKey(userId) }),
        queryClient.invalidateQueries({ queryKey: historyRootKey(userId) }),
      ]),
  });
}
