import { useMutation, useQueryClient } from '@tanstack/react-query';

import { register } from '@/api/auth';

import { acceptAuthenticated } from './authCache';

import type { AuthenticatedResponse, Credentials } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

export const registerMutationKey = ['auth', 'register'] as const;

/**
 * Create an account; a `201` is also a login, so success is handled exactly as `useLogin`'s.
 * A policy refusal carries the server's number in `ApiError.details` (`min_length`, `max_length`)
 * — the page reads it there; nothing here restates the rule (Constitution §4.5).
 */
export function useRegister(): UseMutationResult<AuthenticatedResponse, Error, Credentials> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: registerMutationKey,
    mutationFn: (credentials: Credentials) => register(credentials),
    onSuccess: (response) => {
      acceptAuthenticated(queryClient, response);
    },
  });
}
