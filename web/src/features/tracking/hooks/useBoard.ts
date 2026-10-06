import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';

import { boardQueryOptions } from './boardCache';
import type { BoardCard, Stage } from '../types';

/**
 * What `useBoard` gives a page — a discriminated union, so "loading", "failed" and "here are the
 * cards" cannot render at once (T-38). An empty board is `ready` with no cards: "we could not ask"
 * is never "you have nothing".
 *
 * `byStage` holds every stage (an empty list for an empty column), each list in the server's order
 * (`stage_changed_at DESC, id DESC`) — grouping is presentation, the order is the API's.
 */
export type BoardView =
  | { readonly status: 'loading' }
  | { readonly status: 'error'; readonly retry: () => void }
  | {
      readonly status: 'ready';
      readonly cards: readonly BoardCard[];
      readonly byStage: Readonly<Record<Stage, readonly BoardCard[]>>;
    };

function groupByStage(cards: readonly BoardCard[]): Record<Stage, BoardCard[]> {
  // A literal rather than built from `STAGES`, so the compiler checks every stage has a list
  // without a cast; `Record<Stage, …>` refuses a seventh key or a missing one.
  const groups: Record<Stage, BoardCard[]> = {
    to_apply: [],
    applied: [],
    interviewing: [],
    offer: [],
    rejected: [],
    withdrawn: [],
  };
  for (const card of cards) {
    groups[card.stage].push(card);
  }
  return groups;
}

/**
 * One user's board (plan §7, AC-32): `useQuery` under `boardKey(userId)` — under the account's key
 * root, so logout clears it (AC-39) — grouped by stage, the grouping memoized on the cache entry's
 * `items` (structural sharing keeps that reference stable across an identical refetch).
 *
 * The grouping is derived during render, never stored: the cache entry is the only copy, so an
 * optimistic move written there (`useMoveTrackedApplication`) regroups the columns by itself.
 *
 * Once there is data, a failed *refetch* keeps showing it: the error state is for "we have nothing
 * to show", exactly as `HistoryPage` reads it.
 */
export function useBoard(userId: string): BoardView {
  const query = useQuery(boardQueryOptions(userId));
  const items = query.data?.items;
  const byStage = useMemo(() => (items === undefined ? null : groupByStage(items)), [items]);

  if (items === undefined || byStage === null) {
    if (query.isError) {
      return {
        status: 'error',
        retry: () => {
          void query.refetch();
        },
      };
    }
    return { status: 'loading' };
  }
  return { status: 'ready', cards: items, byStage };
}
