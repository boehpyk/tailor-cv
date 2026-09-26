/**
 * The TanStack keys of slice 2.2's saved-CV hooks, in one place so a hook, its invalidation and a
 * test name each key once.
 *
 * **Why the list lives under `['auth', …]`** (technical plan §7, AC-33). It is the account's data,
 * so it belongs to the signed-in user, and 2.1 already removes every `['auth', …]` query on logout,
 * on a refresh that finds the login gone, and — from this slice — on account deletion and on
 * another tab's sign-out. Putting the list under that prefix makes all four clear it **for free**,
 * instead of each remembering one more key.
 *
 * **Why the user id is part of the key.** Removal happens *after* a sign-out; a same-tab switch from
 * user A to user B is a new login immediately after. Keyed by id, B's list is a different cache
 * entry from A's by construction, so not even one render of B's page can show A's CVs — it would
 * have to find them under B's key, and they are not there.
 */

/** Every saved-CV list, whoever's — what an invalidation after a write matches. */
export const savedBaseCvsQueryKeyPrefix = ['auth', 'savedBaseCvs'] as const;

/**
 * One user's saved-CV list. `userId` is `null` only while the query is disabled (no user known yet),
 * and a disabled query never fetches, so no list is ever cached under `null`.
 */
export function savedBaseCvsQueryKey(
  userId: string | null,
): readonly ['auth', 'savedBaseCvs', string | null] {
  return ['auth', 'savedBaseCvs', userId];
}

/** Mutation keys: `[context, action]`, like 1.1's. The mutation cache is not the query cache. */
export const uploadSavedBaseCvMutationKey = ['auth', 'uploadSavedBaseCv'] as const;
export const renameSavedBaseCvMutationKey = ['auth', 'renameSavedBaseCv'] as const;
export const deleteSavedBaseCvMutationKey = ['auth', 'deleteSavedBaseCv'] as const;
/**
 * Under `intake`, not `auth`: the copy's *result* is a guest `BaseCv` in this browser's workspace,
 * and what it invalidates is `['intake', 'baseCvs']`. It is also the key the Use-this-CV button's
 * same-tick double-click guard reads (`queryClient.isMutating({ mutationKey })`, AC-39).
 */
export const copySavedBaseCvMutationKey = ['intake', 'copySavedBaseCv'] as const;
