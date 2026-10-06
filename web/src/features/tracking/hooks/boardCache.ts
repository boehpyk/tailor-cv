import { queryOptions } from '@tanstack/react-query';

import { fetchBoard } from '@/api/trackedApplications';

import { boardKey } from './trackingKeys';

import type { Board, BoardCard, TrackedApplication } from '../types';

/**
 * The board query and the few pure edits the mutations make to its cache entry (plan §7).
 *
 * **One query definition**, shared by `useBoard` (the page) and `useBoardEntryForRun` (every badge
 * on a history page): same key, same function, so TanStack dedupes them into one request and one
 * cache entry however many observers a page mounts.
 *
 * The edits are pure functions of a `Board`: they never fetch and never decide a rule. Each keeps
 * the server's order (`stage_changed_at DESC, id DESC`) by re-sorting, so a card moved here sits
 * where the next `GET` would put it — the refetch then changes nothing on screen.
 */
export function boardQueryOptions(userId: string) {
  return queryOptions({
    queryKey: boardKey(userId),
    queryFn: ({ signal }): Promise<Board> => fetchBoard(signal),
  });
}

/** The server's order: `stage_changed_at` newest first, then `id` descending. */
function byServerOrder(a: BoardCard, b: BoardCard): number {
  const byTime = Date.parse(b.stage_changed_at) - Date.parse(a.stage_changed_at);
  if (byTime !== 0) {
    return byTime;
  }
  return a.id < b.id ? 1 : a.id > b.id ? -1 : 0;
}

/** The board with one card replaced by `next(card)`, re-sorted. Unchanged if the card is absent. */
export function withCard(board: Board, id: string, next: (card: BoardCard) => BoardCard): Board {
  if (!board.items.some((card) => card.id === id)) {
    return board;
  }
  const items = board.items.map((card) => (card.id === id ? next(card) : card));
  return { items: [...items].sort(byServerOrder) };
}

/** The board without one card. */
export function withoutCard(board: Board, id: string): Board {
  return { items: board.items.filter((card) => card.id !== id) };
}

/**
 * A write's answer merged into its card: the seven fields a write returns replace the card's own,
 * and the three joins (`run`, `posting`, `base_cv`) — which a write does not return — are kept.
 */
export function mergeApplication(card: BoardCard, application: TrackedApplication): BoardCard {
  return {
    ...card,
    stage: application.stage,
    title: application.title,
    tracked_at: application.tracked_at,
    stage_changed_at: application.stage_changed_at,
    version: application.version,
  };
}

/**
 * Whether a mutation's variables name this id — a card's (`{ id }`, a move or a retitle) or a bare
 * string (a card id to untrack, a run id to track). `Mutation.state.variables` is `unknown` to a
 * filter (the cache holds every mutation), so it is narrowed here rather than cast.
 */
export function variablesNameId(variables: unknown, id: string): boolean {
  if (variables === id) {
    return true;
  }
  return (
    typeof variables === 'object' && variables !== null && 'id' in variables && variables.id === id
  );
}
