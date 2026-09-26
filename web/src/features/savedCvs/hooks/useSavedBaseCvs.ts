import { useQuery } from '@tanstack/react-query';
import { useSyncExternalStore } from 'react';

import { fetchSavedBaseCvs } from '@/api/savedBaseCvs';
import { authStore } from '@/features/auth/authStore';
import { useCurrentUser } from '@/features/auth/hooks/useCurrentUser';

import { savedBaseCvsQueryKey } from './savedCvsKeys';

import type { SavedBaseCvList } from '../types';
import type { UseQueryResult } from '@tanstack/react-query';

/**
 * The signed-in user's saved CVs — `GET /api/me/base-cvs` under
 * `['auth', 'savedBaseCvs', userId]` (AC-33; the key's reasoning is in `savedCvsKeys.ts`).
 *
 * **Enabled only while the auth store is `authenticated` and the user's id is known.** A guest has
 * no saved CVs to ask for, and a request sent while `booting` or `unavailable` would only cache a
 * 401 as "the list". The id comes from `['auth', 'me']` (`useCurrentUser`), which a login, a
 * register and the boot refresh all seed — so in practice it is known in the same render the store
 * turns `authenticated`, and the key can never be one user's while the token is another's for more
 * than the instant a `/me` takes.
 *
 * Returns the whole query result, so a component renders loading, error + Retry (`refetch`), empty
 * and success from the request's own state — never from a copy in `useState`.
 *
 * **No automatic retry.** Both surfaces that read this list draw a Retry button on its error state,
 * and asking again costs the user one click. An automatic retry would buy nothing a 4xx can use —
 * `invalid_access_token` has already had its one refresh-and-retry inside `api/client.ts`, and
 * `not_signed_in` has already signed the store out — and would hold a 503 back from the user for a
 * second before saying the same thing.
 */
export function useSavedBaseCvs(): UseQueryResult<SavedBaseCvList> {
  const snapshot = useSyncExternalStore(authStore.subscribe, authStore.getSnapshot);
  const currentUser = useCurrentUser();
  const userId = snapshot.status === 'authenticated' ? (currentUser.data?.id ?? null) : null;

  return useQuery({
    queryKey: savedBaseCvsQueryKey(userId),
    queryFn: ({ signal }) => fetchSavedBaseCvs(signal),
    enabled: userId !== null,
    retry: false,
  });
}
