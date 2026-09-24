import { useMutation, useQueryClient } from '@tanstack/react-query';

import { login } from '@/api/auth';

import { acceptAuthenticated } from './authCache';

import type { AuthenticatedResponse, Credentials } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

export const loginMutationKey = ['auth', 'login'] as const;

/**
 * Log in. The pending and error states are the mutation's own (`isPending`, `error.code`) — the
 * page reads them rather than copying them into `useState`. On success the token goes to the store
 * and the user to `['auth', 'me']` (`acceptAuthenticated`); navigating is the page's job.
 */
export function useLogin(): UseMutationResult<AuthenticatedResponse, Error, Credentials> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: loginMutationKey,
    mutationFn: (credentials: Credentials) => login(credentials),
    onSuccess: (response) => {
      acceptAuthenticated(queryClient, response);
    },
  });
}
