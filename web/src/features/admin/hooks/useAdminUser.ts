import { useQuery } from '@tanstack/react-query';

import { getUser } from '@/api/admin';
import { accountKeyRoot } from '@/features/scope/scopeMap';

import { isClientError } from './useUserDirectory';

import type { AdminUserDetail } from '@/api/admin';
import type { UseQueryResult } from '@tanstack/react-query';

/** Every admin key's root, under the actor's account root, so a sign-out drops it all (AC-38). */
export function adminKey(actorId: string): readonly string[] {
  return [...accountKeyRoot(actorId), 'admin'];
}

/** One account's overview, under the actor's account root (AC-38). */
export function adminUserKey(actorId: string, userId: string): readonly string[] {
  return [...adminKey(actorId), 'user', userId];
}

/** One guest session's overview and content, under the actor's account root (slice 4.4, AC-40). */
export function adminGuestKey(actorId: string, sessionId: string): readonly string[] {
  return [...adminKey(actorId), 'guest', sessionId];
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
