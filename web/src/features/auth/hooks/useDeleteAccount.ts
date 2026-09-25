import { useMutation } from '@tanstack/react-query';

import type { UseMutationResult } from '@tanstack/react-query';

export const deleteAccountMutationKey = ['auth', 'deleteAccount'] as const;

/**
 * Delete the signed-in account with its password (`POST /api/auth/delete-account`).
 *
 * **On success, in this order** (technical plan §7, AC-40): the store goes `anonymous` with reason
 * `account_deleted` — first, so every `['auth', …]` query is disabled before its entry disappears
 * and none refetches it — then every `['auth', …]` query is removed, then the other tabs are told
 * (`authStore.broadcastSignOut()`, AC-42). Navigating to `/` with the notice is the component's job.
 *
 * **On error nothing happens locally.** Every refusal — including 403 `password_incorrect`, which
 * the interceptor never refreshes on (AC-43) — means the account still exists, so the user stays
 * signed in and the section says why. The mutation's variable is the password; it is never stored
 * anywhere but the form's own state and this one request.
 *
 * SKELETON (T25): the real key; the mutation rejects without a request. GREEN is T27.
 */
export function useDeleteAccount(): UseMutationResult<void, Error, string> {
  // A typed value rather than a parameter only while it is a stub (no unused `password`).
  const deleteWithPassword: (password: string) => Promise<void> = () =>
    Promise.reject(new Error('useDeleteAccount: not implemented (T27)'));
  return useMutation({
    mutationKey: deleteAccountMutationKey,
    mutationFn: deleteWithPassword,
  });
}
