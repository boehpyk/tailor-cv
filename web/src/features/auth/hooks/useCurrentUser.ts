import { useQuery } from '@tanstack/react-query';
import { useSyncExternalStore } from 'react';

import { me } from '@/api/auth';
import { ApiError } from '@/api/client';

import { authStore } from '../authStore';

import { currentUserQueryKey } from './authCache';

import type { User } from '../types';
import type { UseQueryResult } from '@tanstack/react-query';

/** One more attempt for a transient failure — the app-wide default, restated because `retry` is a function. */
const MAX_TRANSIENT_RETRIES = 1;

/** A 4xx is the server's answer, not a blip; asking again gets the same answer. */
function isClientError(error: Error): boolean {
  return error instanceof ApiError && error.status >= 400 && error.status < 500;
}

/**
 * The current user's profile — `GET /api/auth/me` under `['auth', 'me']`.
 *
 * **`enabled` only while `authenticated`.** Before the boot answers there is no token to send, and
 * for a guest there never is one; an enabled query would ask anyway and cache a 401 as "the user".
 * Usually the query never fetches at all: a login, a register and the boot refresh all *seed* this
 * key from the token response (`authCache.ts`), and the seed is fresh for the app's `staleTime`.
 *
 * **A 4xx is never retried.** `invalid_access_token` has already had its one refresh-and-retry
 * inside `api/client.ts`; `not_signed_in` means the user is gone. Neither changes on a second ask.
 *
 * Returns the whole query result, so `AccountPage` can render its loading, error and Retry
 * (`refetch`) states from the request's own state rather than a copy of it.
 */
export function useCurrentUser(): UseQueryResult<User> {
  const snapshot = useSyncExternalStore(authStore.subscribe, authStore.getSnapshot);

  return useQuery({
    queryKey: currentUserQueryKey,
    queryFn: ({ signal }) => me(signal),
    enabled: snapshot.status === 'authenticated',
    retry: (failureCount, error) => !isClientError(error) && failureCount < MAX_TRANSIENT_RETRIES,
  });
}
