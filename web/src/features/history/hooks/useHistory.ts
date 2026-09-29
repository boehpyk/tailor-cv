import { useInfiniteQuery } from '@tanstack/react-query';

import { historyPagesKey } from './historyKeys';

import type { HistoryPage } from '../types';
import type { InfiniteData, UseInfiniteQueryResult } from '@tanstack/react-query';

/** What `useHistory` returns: the pages so far, keyed by the cursor each was fetched with. */
export type HistoryQuery = UseInfiniteQueryResult<InfiniteData<HistoryPage, string | null>>;

/**
 * One user's history, page by page (plan §7): `useInfiniteQuery` under `historyPagesKey(userId)`,
 * `fetchHistoryPage(cursor)`, the first page's cursor `null`, and `getNextPageParam =
 * next_cursor ?? undefined` — so *Load more* exists iff the server said there is more (AC-44). A
 * later page's failure keeps the loaded pages (H-59).
 *
 * SKELETON (T29): the real key and return type; never fetches. T31 implements it.
 */
export function useHistory(userId: string): HistoryQuery {
  return useInfiniteQuery({
    queryKey: historyPagesKey(userId),
    queryFn: (): Promise<HistoryPage> =>
      Promise.reject(new Error('useHistory: not implemented (T31)')),
    initialPageParam: null as string | null,
    getNextPageParam: () => undefined,
    enabled: false,
  });
}
