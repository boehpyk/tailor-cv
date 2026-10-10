import { useQuery } from '@tanstack/react-query';

import { adminAccess } from '@/api/admin';
import { ApiError } from '@/api/client';
import { accountKeyRoot } from '@/features/scope/scopeMap';

import type { UseQueryResult } from '@tanstack/react-query';

/** One more attempt for a transient failure — the app-wide default, restated because `retry` is a function. */
const MAX_TRANSIENT_RETRIES = 1;

/** A 4xx is the server's answer: a 404 here means "not an admin", and asking again says it again. */
function isClientError(error: Error): boolean {
  return error instanceof ApiError && error.status >= 400 && error.status < 500;
}

/** The probe's key, under the account's root so every sign-out drops it (AC-34). */
export function adminAccessQueryKey(userId: string): readonly string[] {
  return [...accountKeyRoot(userId), 'admin', 'access'];
}

/**
 * May this account see `/admin`? — `GET /api/admin/access`, asked of the server every time the
 * screen mounts, never inferred from the cached `user.role` (AC-34, OQ-13): the role in the client
 * is a hint for showing a link, not authority.
 *
 * **Refetch on every window focus, `'always'`** — set here because the app's client turns focus
 * refetch off (`queryClient.ts`), and `'always'` because `true` skips a refetch while the cached
 * 204 is inside the app's 30 s `staleTime`. That is how a demotion is noticed: the next focus
 * answers 404, and the view puts that error ahead of the cached 204 (R-26). The probe is one
 * primary-key read answering an empty 204, so asking on each focus is cheap (AC-38). F-1 (T26)
 * found the earlier "stays on (the TanStack default)" assumption false.
 *
 * A 404 is not retried (AC-31); a 503 or a network failure is retried once.
 *
 * Takes the `userId` rather than reading the scope, so a guest scope cannot reach it by type.
 *
 * The data is `true`, not the client's `void`: TanStack v5 refuses a query function that resolves
 * `undefined` and turns the 204 into an error — which the view would show as "unavailable".
 */
export function useAdminAccess(userId: string): UseQueryResult<true> {
  return useQuery({
    queryKey: adminAccessQueryKey(userId),
    queryFn: async ({ signal }) => {
      await adminAccess(signal);
      return true as const;
    },
    retry: (failureCount, error) => !isClientError(error) && failureCount < MAX_TRANSIENT_RETRIES,
    refetchOnWindowFocus: 'always',
  });
}
