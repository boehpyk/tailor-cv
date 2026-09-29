import { useQuery } from '@tanstack/react-query';

import { latestAccountRunKey } from './historyKeys';

import type { HistoryEntry } from '../types';
import type { UseQueryResult } from '@tanstack/react-query';

/**
 * The account's newest run, for the workspace's latest-run card (AC-41): `GET
 * /api/me/tailoring-runs?limit=1` under `latestAccountRunKey(userId)`, answering the entry or
 * `null` when the history is empty. Server state, never copied into `useState`.
 *
 * SKELETON (T29): the real key and return type; never fetches. T31 implements it.
 */
export function useLatestAccountRun(userId: string): UseQueryResult<HistoryEntry | null> {
  return useQuery({
    queryKey: latestAccountRunKey(userId),
    queryFn: (): Promise<HistoryEntry | null> =>
      Promise.reject(new Error('useLatestAccountRun: not implemented (T31)')),
    enabled: false,
  });
}
