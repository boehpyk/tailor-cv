import { useMutation } from '@tanstack/react-query';

import { confirmPasswordReset } from '@/api/auth';

import type { PasswordResetConfirmation } from '@/api/auth';
import type { UseMutationResult } from '@tanstack/react-query';

export const confirmPasswordResetMutationKey = ['auth', 'confirm-password-reset'] as const;

/**
 * Set a new password with a reset link's token. On the server a `204` revokes every login of the
 * account, this tab's included (ADR-0028); signing an authenticated tab out with reason
 * `password_changed` is the screen's success path (AC-48), not a transport concern.
 */
export function useConfirmPasswordReset(): UseMutationResult<
  void,
  Error,
  PasswordResetConfirmation
> {
  return useMutation({
    mutationKey: confirmPasswordResetMutationKey,
    mutationFn: (confirmation: PasswordResetConfirmation) => confirmPasswordReset(confirmation),
  });
}
