import { useInfiniteQuery } from '@tanstack/react-query';

import { fetchHistoryPage } from '@/api/history';

import { historyPagesKey } from './historyKeys';

import type { HistoryPage } from '../types';
import type { InfiniteData, UseInfiniteQueryResult } from '@tanstack/react-query';

/** What `useHistory` returns: the pages so far, keyed by the cursor each was fetched with. */
export type HistoryQuery = UseInfiniteQueryResult<InfiniteData<HistoryPage, string | null>>;

/**
 * One user's history, page by page (plan §7, AC-44): `useInfiniteQuery` under
 * `historyPagesKey(userId)` — under the account's key root, so logout clears it (AC-43).
 *
 * - The first page's cursor is `null`; each next one is the previous page's `next_cursor`, passed
 *   through untouched. `getNextPageParam` returns `undefined` on the last page, which is what makes
 *   `hasNextPage` false and *Load more* disappear — the server, not the client, says whether there
 *   is more.
 * - **A failed later page keeps the loaded ones** (H-59): TanStack keeps `data` and sets
 *   `isFetchNextPageError`, so the page can tell "page 1 failed" (no data) from "page N failed"
 *   (data, plus that flag) and offer Retry for that page alone.
 * - No custom retry: the app default (none in tests, TanStack's in the browser) is right for a read.
 */
export function useHistory(userId: string): HistoryQuery {
  return useInfiniteQuery({
    queryKey: historyPagesKey(userId),
    queryFn: ({ pageParam, signal }) => fetchHistoryPage(pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
}
