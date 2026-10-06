import { accountKeyRoot } from '@/features/scope/scopeMap';

/**
 * The board's query and mutation keys (technical plan §7, AC-39). **Every one is under the
 * account's key root** (`['auth', 'account', userId, 'tracking', …]`), so 2.1's
 * `removeQueries(['auth'])` on logout, account deletion and a cross-tab sign-out clears a user's
 * board from memory with no new code, and user B's board is a different entry from user A's by
 * construction.
 *
 * Mutation keys carry the user id too (unlike history's one shared delete key): AC-39 asks that
 * every tracking key sit under the account root, and the `isMutating` guard against a same-tick
 * double choice (T-29) reads them.
 */

/** Everything tracking-shaped for one user. */
export function trackingRootKey(userId: string): readonly string[] {
  return [...accountKeyRoot(userId), 'tracking'];
}

/** `GET /api/me/board` — the one board query; badges on history rows `select` over it. */
export function boardKey(userId: string): readonly string[] {
  return [...trackingRootKey(userId), 'board'];
}

/** `POST /api/me/tracked-applications` — *Add to board*. */
export function trackApplicationMutationKey(userId: string): readonly string[] {
  return [...trackingRootKey(userId), 'track'];
}

/** `PUT …/{id}/stage` — the optimistic move, by control or by drag (one key for both). */
export function moveTrackedApplicationMutationKey(userId: string): readonly string[] {
  return [...trackingRootKey(userId), 'move'];
}

/** `PUT …/{id}/title`. */
export function retitleTrackedApplicationMutationKey(userId: string): readonly string[] {
  return [...trackingRootKey(userId), 'retitle'];
}

/** `DELETE …/{id}` — *Remove from board*. */
export function untrackApplicationMutationKey(userId: string): readonly string[] {
  return [...trackingRootKey(userId), 'untrack'];
}
