import { useMutation } from '@tanstack/react-query';

import { confirmRegistration } from '@/api/auth';

import type { UseMutationResult } from '@tanstack/react-query';

export const confirmRegistrationMutationKey = ['auth', 'confirm-registration'] as const;

/**
 * Confirm a pending registration with the token from the link's fragment. A `204` creates the
 * account but does **not** sign in (plan §0.6), so — like the request — nothing here touches the
 * auth store. The variable is the token, held by `useFragmentToken` in memory only; TanStack keeps
 * a mutation's variables on the mutation, never in the query cache.
 */
export function useConfirmRegistration(): UseMutationResult<void, Error, string> {
  return useMutation({
    mutationKey: confirmRegistrationMutationKey,
    mutationFn: (token: string) => confirmRegistration(token),
  });
}
