import { useQuery } from '@tanstack/react-query';

import { fetchHistoryPage } from '@/api/history';

import { latestAccountRunKey } from './historyKeys';

import type { HistoryEntry } from '../types';
import type { UseQueryResult } from '@tanstack/react-query';

/**
 * The account's newest run, for the workspace's latest-run card (AC-41): `GET
 * /api/me/tailoring-runs?limit=1` under `latestAccountRunKey(userId)`, answering the entry or
 * `null` for an empty history. Server state — never copied into `useState`.
 *
 * Under the history root, so a new run, an autosave and a deletion (which all invalidate that
 * root) refresh the card with no code of their own. It does not poll: the run page does, and the
 * card is the way there.
 */
export function useLatestAccountRun(userId: string): UseQueryResult<HistoryEntry | null> {
  return useQuery({
    queryKey: latestAccountRunKey(userId),
    queryFn: async ({ signal }) => {
      const page = await fetchHistoryPage(null, signal, 1);
      return page.items[0] ?? null;
    },
  });
}
