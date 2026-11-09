import { useMutation, useMutationState, useQueryClient } from '@tanstack/react-query';

import { postRunFitScore } from '@/api/tailoringRuns';
import { GUEST_SCOPE_MAP } from '@/features/scope/scopeMap';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';

import { runFitScoresQueryKey } from './useRunFitScores';

import type { FitScoreSide, RunFitScores } from '../types';
import type { ScopeMap } from '@/features/scope/scopeMap';
import type { Mutation } from '@tanstack/react-query';

/** One run's comparison writes. The side is the mutation's variables, so filters read it. */
export function compareScoresMutationKey(
  runId: string,
  map: Pick<ScopeMap, 'keyRoot'> = GUEST_SCOPE_MAP,
): readonly unknown[] {
  return [...map.keyRoot, 'scoring', 'compareScores', runId];
}

/** The latest request for one side of this run, from the mutation cache. */
export type SideRequest =
  | { readonly status: 'idle' }
  | { readonly status: 'pending' }
  | { readonly status: 'error'; readonly error: Error }
  | { readonly status: 'success' };

export interface CompareScores {
  /**
   * Score each named side that is not already in flight for this run, **in parallel**. Call it from
   * a click only: each side not stored yet is a paid Gemini call.
   */
  readonly compare: (sides: readonly FitScoreSide[]) => void;
  /** Each side's latest request. A stored score is the read's (`useRunFitScores`), not this. */
  readonly sides: Readonly<Record<FitScoreSide, SideRequest>>;
}

const IDLE: SideRequest = { status: 'idle' };

function sideOf(mutation: Mutation): FitScoreSide | undefined {
  return mutation.state.variables as FitScoreSide | undefined;
}

/**
 * *Compare scores* (slice 5.2, AC-18, AC-37): fire `before` and `after` in parallel, each guarded
 * against a same-tick double click by `queryClient.isMutating`, filtered to this run (the key) and
 * this side (the variables). `isPending` lags a tick, and one `useMutation` observer follows only
 * its latest `mutate`, so per-side state is read from the mutation cache with `useMutationState`.
 *
 * - **No `retry`**: a retried `POST` is a second paid call (`retry.grep.test.ts`).
 * - **`onSuccess`** writes the side's score into the run's read **only if it exists** (a sign-out
 *   mid-call removed it; the gaps are CV-derived PII, ADR-0040), then invalidates it without
 *   returning, so the server recomputes `comparable` and the verdict once both sides are stored.
 * - **`onError`** invalidates the read: a `POST` that failed on the wire but finished on the server
 *   shows its stored row.
 *
 * Both callbacks are the hook's, so they still run when the panel unmounts mid-call.
 */
export function useCompareScores(runId: string): CompareScores {
  const queryClient = useQueryClient();
  const map = useScopeMap();
  const mutationKey = compareScoresMutationKey(runId, map);
  const readKey = runFitScoresQueryKey(runId, map);

  const mutation = useMutation({
    mutationKey,
    mutationFn: (side: FitScoreSide) => postRunFitScore(runId, side),
    onSuccess: (score, side) => {
      if (queryClient.getQueryCache().find({ queryKey: readKey, exact: true }) !== undefined) {
        queryClient.setQueryData<RunFitScores>(readKey, (old) =>
          old === undefined ? old : { ...old, [side]: score },
        );
      }
      void queryClient.invalidateQueries({ queryKey: readKey, exact: true });
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: readKey, exact: true });
    },
  });

  const requests = useMutationState({
    filters: { mutationKey },
    select: (m) => ({ side: sideOf(m), status: m.state.status, error: m.state.error }),
  });

  const sides: Record<FitScoreSide, SideRequest> = { before: IDLE, after: IDLE };
  // In cache order, oldest first: the last one per side is its latest request.
  for (const r of requests) {
    if (r.side === undefined) {
      continue;
    }
    sides[r.side] =
      r.status === 'pending'
        ? { status: 'pending' }
        : r.status === 'error' && r.error !== null
          ? { status: 'error', error: r.error }
          : r.status === 'success'
            ? { status: 'success' }
            : IDLE;
  }

  function compare(wanted: readonly FitScoreSide[]): void {
    for (const side of wanted) {
      const inFlight = queryClient.isMutating({
        mutationKey,
        predicate: (m) => sideOf(m) === side,
      });
      if (inFlight === 0) {
        mutation.mutate(side);
      }
    }
  }

  return { compare, sides };
}
