import { queryOptions, replaceEqualDeep } from '@tanstack/react-query';

import { fetchBoard } from '@/api/trackedApplications';

import { boardKey, moveTrackedApplicationMutationKey } from './trackingKeys';

import type { QueryClient } from '@tanstack/react-query';
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
    structuralSharing: shareCardsById,
  });
}

/**
 * Narrows a cache value to a `Board`. TanStack hands `structuralSharing` `unknown`; the only values
 * ever written under the board key are `fetchBoard`'s and this module's edits, so this is a type
 * narrowing for the compiler, not a validation of the wire (the API client owns that).
 */
function isBoard(value: unknown): value is Board {
  return (
    typeof value === 'object' && value !== null && 'items' in value && Array.isArray(value.items)
  );
}

/**
 * **Structural sharing by card id, not by position** (AC-44). TanStack's default,
 * `replaceEqualDeep`, compares arrays index by index — and a move re-sorts the board, so every card
 * between the moved card's old and new place sits at a new index and comes back as a fresh copy.
 * `BoardCard` is memoized on its `card` object, so a fresh copy re-renders it: a move on a 500-card
 * board re-rendered all 500. Matching each card to the previous card with its id keeps every
 * unchanged card the very object it was — through an optimistic edit and through a refetch alike —
 * so a move re-renders only the card that changed.
 *
 * Each pair is still shared deeply (`replaceEqualDeep`), and the whole previous board is returned
 * when nothing changed, so an identical refetch notifies nobody — the default's guarantee, kept.
 */
export function shareCardsById(previous: unknown, next: unknown): unknown {
  if (!isBoard(previous) || !isBoard(next)) {
    return replaceEqualDeep(previous, next);
  }
  const previousById = new Map(previous.items.map((card) => [card.id, card]));
  let unchanged = previous.items.length === next.items.length;
  const items = next.items.map((card, index) => {
    const before = previousById.get(card.id);
    const shared = before === undefined ? card : replaceEqualDeep(before, card);
    if (shared !== previous.items[index]) {
      unchanged = false;
    }
    return shared;
  });
  return unchanged ? previous : { ...next, items };
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

/**
 * Re-read the board after a write that is not a move (a removal, a retitle) — **unless a move is in
 * flight**. A refetch then would land the server's board, which has not seen that move yet, on top
 * of its optimistic state: the moving card would flicker back to its old column. The write's own
 * answer is already in the cache entry, and the last move to settle re-reads the board for
 * everything that happened meanwhile (`useMoveTrackedApplication`'s `onSettled`), so deferring
 * loses nothing and costs exactly one read.
 */
export function invalidateBoardUnlessMoving(queryClient: QueryClient, userId: string): void {
  if (queryClient.isMutating({ mutationKey: moveTrackedApplicationMutationKey(userId) }) > 0) {
    return;
  }
  void queryClient.invalidateQueries({ queryKey: boardKey(userId) });
}
