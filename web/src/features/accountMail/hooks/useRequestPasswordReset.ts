import { useMutation } from '@tanstack/react-query';

import { requestPasswordReset } from '@/api/auth';

import type { UseMutationResult } from '@tanstack/react-query';

export const requestPasswordResetMutationKey = ['auth', 'password-reset'] as const;

/**
 * Ask for a reset link for an email address. The `202` is the same whether or not the address has
 * an account, so the success copy is neutral (*"If there's an account for…"*) and nothing here can
 * learn otherwise.
 */
export function useRequestPasswordReset(): UseMutationResult<void, Error, string> {
  return useMutation({
    mutationKey: requestPasswordResetMutationKey,
    mutationFn: (email: string) => requestPasswordReset(email),
  });
}
