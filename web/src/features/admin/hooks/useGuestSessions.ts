import { useInfiniteQuery, useQuery } from '@tanstack/react-query';

import { getGuestSession, listGuestSessions } from '@/api/admin';

import { adminGuestKey, adminKey } from './useAdminUser';
import { isClientError } from './useUserDirectory';

import type { AdminGuestSessionDetail, GuestSessionPage } from '@/api/admin';
import type { InfiniteData, UseInfiniteQueryResult, UseQueryResult } from '@tanstack/react-query';

/** The guest list's pages — a sibling of `adminGuestKey`'s root, so an erase's prefix removal of
 * one session never drops the list; the list is invalidated instead (AC-40). */
export function guestListKey(actorId: string): readonly string[] {
  return [...adminKey(actorId), 'guests'];
}

/** A 4xx (gone, firewall, bad cursor) is an answer; a 503 or a network failure is retried once. */
function retryOnce(failureCount: number, error: Error): boolean {
  return !isClientError(error) && failureCount < 1;
}

export type GuestSessionsQuery = UseInfiniteQueryResult<
  InfiniteData<GuestSessionPage, string | null>
>;

/** Live guest sessions, page by page (AC-32); the server says whether there is more. */
export function useGuestSessions(actorId: string): GuestSessionsQuery {
  return useInfiniteQuery({
    queryKey: guestListKey(actorId),
    queryFn: ({ pageParam, signal }) => listGuestSessions(pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: retryOnce,
  });
}

/** One session's overview and content lists (AC-33), at the session's own key root. */
export function useGuestSession(
  actorId: string,
  sessionId: string,
): UseQueryResult<AdminGuestSessionDetail> {
  return useQuery({
    queryKey: adminGuestKey(actorId, sessionId),
    queryFn: ({ signal }) => getGuestSession(sessionId, signal),
    retry: retryOnce,
  });
}
