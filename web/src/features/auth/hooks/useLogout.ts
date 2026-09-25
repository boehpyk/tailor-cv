import { useMutation, useQueryClient } from '@tanstack/react-query';

import { logout } from '@/api/auth';

import { authStore } from '../authStore';

import { authQueryKeyPrefix } from './authCache';

import type { UseMutationResult } from '@tanstack/react-query';

export const logoutMutationKey = ['auth', 'logout'] as const;

/**
 * Log out — the server first, then this tab.
 *
 * **On error the user stays logged in** (I-31). A 503 means the login was not ended and the cookie
 * was not cleared, so signing out locally would show "logged out" to someone whose next page load
 * logs them straight back in. Nothing happens in `onError`; the page shows `error`.
 *
 * **On success, only `['auth', …]` is removed** (AC-44). The guest workspace's queries stay: that
 * work belongs to this browser's guest session, not to the user who just left. `signOut` goes
 * first, so `useCurrentUser` is disabled before its cache entry disappears and does not refetch it.
 */
export function useLogout(): UseMutationResult<void, Error, void> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: logoutMutationKey,
    mutationFn: () => logout(),
    onSuccess: () => {
      authStore.signOut('logged_out');
      queryClient.removeQueries({ queryKey: authQueryKeyPrefix });
    },
  });
}
