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
 * **Refetch on window focus stays on** (the TanStack default). That is how a demotion is noticed:
 * the next focus answers 404, and the view puts that error ahead of the cached 204 (R-26).
 *
 * A 404 is not retried (AC-31); a 503 or a network failure is retried once.
 *
 * Takes the `userId` rather than reading the scope, so a guest scope cannot reach it by type.
 */
export function useAdminAccess(userId: string): UseQueryResult<void> {
  return useQuery({
    queryKey: adminAccessQueryKey(userId),
    queryFn: ({ signal }) => adminAccess(signal),
    retry: (failureCount, error) => !isClientError(error) && failureCount < MAX_TRANSIENT_RETRIES,
  });
}
