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
 * `onDeleted` runs **before** any of that. It is where the page navigates away, and it has to go
 * first: the sign-out re-renders `RequireAuth`, whose answer for an anonymous visitor to `/account`
 * is "go and log in" — a page that only navigated after the sign-out would already be unmounted,
 * and a mutate-level `onSuccess` on an unmounted observer never runs.
 *
 * **On error nothing happens locally.** Every refusal — including 403 `password_incorrect`, which
 * the interceptor never refreshes on (AC-43) — means the account still exists, so the user stays
 * signed in and the section says why. The mutation's variable is the password; it is never stored
 * anywhere but the form's own state and this one request.
 */
export function useDeleteAccount(onDeleted: () => void): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: deleteAccountMutationKey,
    mutationFn: (password: string) => deleteAccount(password),
    onSuccess: () => {
      onDeleted();
      authStore.signOut('account_deleted');
      queryClient.removeQueries({ queryKey: authQueryKeyPrefix });
      authStore.broadcastSignOut();
    },
  });
}
