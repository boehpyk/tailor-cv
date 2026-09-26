import { useMutation, useQueryClient } from '@tanstack/react-query';

import { deleteAccount } from '@/api/auth';

import { authStore } from '../authStore';

import { authQueryKeyPrefix } from './authCache';

import type { UseMutationResult } from '@tanstack/react-query';

export const deleteAccountMutationKey = ['auth', 'deleteAccount'] as const;

/**
 * Delete the signed-in account with its password (`POST /api/auth/delete-account`).
 *
 * **On success, in this order** (technical plan §7, AC-40): the store goes `anonymous` with reason
 * `account_deleted` — first, so every `['auth', …]` query is disabled before its entry disappears
 * and none refetches it — then every `['auth', …]` query is removed, then the other tabs are told
 * (`authStore.broadcastSignOut()`, AC-42). `useLogout`'s order, plus the broadcast.
 *
 * `onDeleted` runs — and is awaited — before any of that: it is where the section navigates to
 * `/` with the notice. It runs from the mutation's own `onSuccess`, not a mutate-level one, because
 * the sign-out unmounts `/account` and TanStack skips mutate-level callbacks on an unmounted observer.
 *
 * **It does not win the race on its own, and `RequireAuth` is what makes the outcome certain.**
 * Measured against the real route table (T28): a data router commits its navigation in a
 * transition, so even an awaited `navigate('/')` has not been rendered when the synchronous
 * sign-out re-renders the still-mounted `RequireAuth`, whose anonymous answer used to be
 * `/login?next=/account`. `RequireAuth` now answers the reason `account_deleted` with `/` and the
 * same notice, so both paths land on the same place whichever renders last.
 *
 * **On error nothing happens locally.** Every refusal — including 403 `password_incorrect`, which
 * the interceptor never refreshes on (AC-43) — means the account still exists, so the user stays
 * signed in and the section says why. The mutation's variable is the password; it is never stored
 * anywhere but the form's own state and this one request.
 */
export function useDeleteAccount(
  onDeleted: () => Promise<void> | void,
): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: deleteAccountMutationKey,
    mutationFn: (password: string) => deleteAccount(password),
    onSuccess: async () => {
      await onDeleted();
      authStore.signOut('account_deleted');
      queryClient.removeQueries({ queryKey: authQueryKeyPrefix });
      authStore.broadcastSignOut();
    },
  });
}
