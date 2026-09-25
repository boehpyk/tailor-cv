import { useQuery } from '@tanstack/react-query';

import { savedBaseCvsQueryKey } from './savedCvsKeys';

import type { SavedBaseCvList } from '../types';
import type { UseQueryResult } from '@tanstack/react-query';

/**
 * The signed-in user's saved CVs — `GET /api/me/base-cvs` under
 * `['auth', 'savedBaseCvs', userId]` (AC-33; the key's reasoning is in `savedCvsKeys.ts`).
 *
 * **Enabled only while the auth store is `authenticated` and the user's id is known.** A guest has
 * no saved CVs to ask for, and a request sent while `booting` or `unavailable` would only cache a
 * 401 as "the list". Returns the whole query result, so a component renders loading, error + Retry
 * (`refetch`), empty and success from the request's own state — never from a copy in `useState`.
 *
 * SKELETON (T25): a query that is never enabled, so it never fetches. GREEN is T27.
 */
export function useSavedBaseCvs(): UseQueryResult<SavedBaseCvList> {
  return useQuery({
    queryKey: savedBaseCvsQueryKey(null),
    queryFn: () => Promise.reject(new Error('useSavedBaseCvs: not implemented (T27)')),
    enabled: false,
  });
}
