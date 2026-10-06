/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the parameters are the signatures qa's T27 tests compile against; T28 uses them and deletes this line. */
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

/**
 * One user's board (plan §7, AC-32): `useQuery` under `boardKey(userId)` — under the account's key
 * root, so logout clears it (AC-39) — grouped by stage, the grouping memoized.
 *
 * SKELETON (T26): no query; always `loading`. T28 implements it.
 */
export function useBoard(_userId: string): BoardView {
  return { status: 'loading' };
}
