import { useQuery } from '@tanstack/react-query';

import { getUser } from '@/api/admin';
import { accountKeyRoot } from '@/features/scope/scopeMap';

import { isClientError } from './useUserDirectory';

import type { AdminUserDetail } from '@/api/admin';
import type { UseQueryResult } from '@tanstack/react-query';

/** One account's overview, under the actor's account root (AC-38). */
export function adminUserKey(actorId: string, userId: string): readonly string[] {
  return [...accountKeyRoot(actorId), 'admin', 'user', userId];
}

/**
 * One account and its counts (AC-33). A 404 `user_not_found` (gone) and the firewall's code-less
 * 404 (demoted) are answers, not retried; a 503 or a network failure is retried once.
 */
export function useAdminUser(actorId: string, userId: string): UseQueryResult<AdminUserDetail> {
  return useQuery({
    queryKey: adminUserKey(actorId, userId),
    queryFn: ({ signal }) => getUser(userId, signal),
    retry: (failureCount, error) => !isClientError(error) && failureCount < 1,
  });
}
