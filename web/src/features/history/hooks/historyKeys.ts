import { accountKeyRoot, scopeMap } from '@/features/scope/scopeMap';

/**
 * The history's query and mutation keys (plan §7). Every one is under the account's key root
 * (`['auth', 'account', userId, …]`), so 2.1's `removeQueries(['auth'])` on logout, account
 * deletion and a cross-tab sign-out clears a user's history from memory with no new code (AC-43).
 *
 * `historyRootKey` is the account scope's `runListKey` — the one key a run's writes invalidate
 * (a new run, an autosave's `edited`, a deletion) — and both history reads live under it.
 */

/** Everything history-shaped for one user: every page and the latest-run card. */
export function historyRootKey(userId: string): readonly unknown[] {
  return scopeMap({ kind: 'account', userId }).runListKey;
}

/** The paged list (`useInfiniteQuery`). */
export function historyPagesKey(userId: string): readonly unknown[] {
  return [...historyRootKey(userId), 'pages'];
}

/** The workspace's latest-run card: `GET /api/me/tailoring-runs?limit=1`. */
export function latestAccountRunKey(userId: string): readonly unknown[] {
  return [...historyRootKey(userId), 'latest'];
}

/**
 * The delete mutation — one key for every user, as the plan names it: the `isMutating` guard
 * against a same-tick double click reads it (H-58), and there is only ever one signed-in user in a
 * tab. Under `['auth', 'account']` all the same.
 */
export const deleteHistoryEntryMutationKey = ['auth', 'account', 'deleteHistoryEntry'] as const;

/** Re-exported so callers that build an account key read the root from one place. */
export { accountKeyRoot };
