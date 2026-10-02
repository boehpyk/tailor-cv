import { useMutation } from '@tanstack/react-query';

import { requestRegistration } from '@/api/auth';

import type { Credentials } from '@/features/auth/types';
import type { UseMutationResult } from '@tanstack/react-query';

export const requestRegistrationMutationKey = ['auth', 'registration'] as const;

/**
 * Ask for an account (slice 2.5). A `202` is **not** a login: nothing reaches the auth store or the
 * query cache, and success is the page's to render (*"Check your inbox…"*). The credentials stay in
 * the page's own state for *Send it again* — never here, never in the cache (AC-51).
 *
 * Guard a submit with `queryClient.isMutating({ mutationKey: requestRegistrationMutationKey })`,
 * not `isPending`, which lags a same-tick double click (2.1's trap).
 */
export function useRequestRegistration(): UseMutationResult<void, Error, Credentials> {
  return useMutation({
    mutationKey: requestRegistrationMutationKey,
    mutationFn: (credentials: Credentials) => requestRegistration(credentials),
  });
}
