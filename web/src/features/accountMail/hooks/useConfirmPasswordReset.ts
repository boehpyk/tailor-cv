import { useMutation, useQueryClient } from '@tanstack/react-query';

import { confirmPasswordReset } from '@/api/auth';
import { authStore } from '@/features/auth/authStore';
import { authQueryKeyPrefix } from '@/features/auth/hooks/authCache';

import type { PasswordResetConfirmation } from '@/api/auth';
import type { UseMutationResult } from '@tanstack/react-query';

export const confirmPasswordResetMutationKey = ['auth', 'confirm-password-reset'] as const;

/**
 * Set a new password with a reset link's token. The request carries no bearer: the token is the
 * whole proof, and an access token in this tab — possibly someone else's — proves nothing here
 * (V-65).
 *
 * **On a `204` the server has revoked every login of the account** (ADR-0028), this tab's included
 * if it was signed in. So, when this tab is `authenticated`, it follows `useDeleteAccount`'s order:
 * the store goes `anonymous` with reason `password_changed` — first, so every `['auth', …]` query
 * is disabled before its entry disappears and none refetches — then every `['auth', …]` query is
 * removed (the guest's stay: that work belongs to this browser), then the other tabs are told
 * (2.2's `BroadcastChannel`). **No refresh and no logout call**: the login is already gone, so a
 * refresh could only answer 401 and a logout has nothing to end.
 *
 * This runs in the mutation's own `onSuccess`, not a mutate-level one: TanStack skips a
 * mutate-level callback on an unmounted observer (CLAUDE.md, 2.2), and the sign-out must happen
 * whether or not the page is still on screen when the answer lands. A tab that is `anonymous`,
 * `booting` or `unavailable` is left alone: it holds no token to drop.
 */
export function useConfirmPasswordReset(): UseMutationResult<
  void,
  Error,
  PasswordResetConfirmation
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: confirmPasswordResetMutationKey,
    mutationFn: (confirmation: PasswordResetConfirmation) => confirmPasswordReset(confirmation),
    onSuccess: () => {
      if (authStore.getSnapshot().status !== 'authenticated') {
        return;
      }
      authStore.signOut('password_changed');
      queryClient.removeQueries({ queryKey: authQueryKeyPrefix });
      authStore.broadcastSignOut();
    },
  });
}
