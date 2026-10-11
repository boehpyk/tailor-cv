import { useInfiniteQuery } from '@tanstack/react-query';

import { listUsers } from '@/api/admin';
import { ApiError } from '@/api/client';
import { accountKeyRoot } from '@/features/scope/scopeMap';

import type { UserDirectoryPage } from '@/api/admin';
import type { InfiniteData, UseInfiniteQueryResult } from '@tanstack/react-query';

/** A 4xx is the server's answer (`invalid_search`, the firewall's 404): asking again says it again. */
export function isClientError(error: Error): boolean {
  return error instanceof ApiError && error.status >= 400 && error.status < 500;
}

/** Every directory page for every search — what an account action invalidates (AC-38). */
export function userDirectoryRootKey(actorId: string): readonly string[] {
  return [...accountKeyRoot(actorId), 'admin', 'users'];
}

/** One search's pages. `q` is `''` for everyone. */
export function userDirectoryKey(actorId: string, q: string): readonly string[] {
  return [...userDirectoryRootKey(actorId), q];
}

export type UserDirectoryQuery = UseInfiniteQueryResult<
  InfiniteData<UserDirectoryPage, string | null>
>;

/**
 * The user directory, page by page, for one search (AC-31, AC-32). `q` comes from the URL
 * (`?q=`, read by `UserListPage`), so a new search is a new key starting at page 1 and back/forward
 * restore it with no state of our own. Keyed under the actor's account root, so a sign-out drops
 * every email loaded here (AC-38). The server, not the client, says whether there is more.
 */
export function useUserDirectory(actorId: string, q: string): UserDirectoryQuery {
  return useInfiniteQuery({
    queryKey: userDirectoryKey(actorId, q),
    queryFn: ({ pageParam, signal }) => listUsers(q, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: (failureCount, error) => !isClientError(error) && failureCount < 1,
  });
}
