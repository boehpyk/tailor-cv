import { useQuery } from '@tanstack/react-query';

import { fetchTailoringRun } from '@/api/tailoringRuns';

import { isActiveTailoringRunStatus } from '../types';

import type { TailoringRun } from '../types';

/** The cache key for one run. `null` is a legal member: it is the key of the disabled query. */
export function tailoringRunQueryKey(runId: string | null) {
  return ['tailoring', 'tailoringRun', runId] as const;
}

/** How often to re-read a run that is still `queued` or `running`. */
const POLL_INTERVAL_MS = 1000;

/**
 * Watch one tailoring run until it finishes — the poller.
 *
 * **`refetchInterval` returning `false` is the polling loop.** TanStack Query re-evaluates the
 * function after every fetch, against the data that fetch produced. While the run is `queued` or
 * `running` it answers `1000` and the query re-reads the run a second later; the moment a response
 * says `succeeded` or `failed`, it answers `false` and there is no next request (AC-30). The loop
 * stops *because the data says it is over*, which is the only thing that should stop it.
 *
 * That is why there is no `useEffect`, no `setInterval` and no cleanup function in this file. A
 * hand-rolled timer has to be cleared on every path out — the terminal status, the unmount, the id
 * changing, the tab going to the background — and the one path someone forgets is a timer that keeps
 * hitting the API after the run is done, or keeps waking a background tab and burning a battery for
 * a result nobody is looking at. Both are exactly what AC-30 forbids. Here the timer belongs to the
 * query observer, which stops it on unmount and on a key change without being asked.
 *
 * `refetchIntervalInBackground: false` covers the background tab: a hidden tab is not polled, and
 * TanStack refetches on focus, so the user sees the current state the moment they come back.
 *
 * Before the first response `data` is `undefined`, the interval is `false`, and that is correct:
 * the initial fetch is not driven by the interval, and nothing is known yet to poll for. After a
 * refetch *fails*, TanStack keeps the last good `data`, so a run last seen `running` keeps being
 * polled — a network blip mid-run is not the end of the run.
 *
 * **No `!` on the id.** `enabled: runId !== null` means `queryFn` will not *normally* run with a
 * null id, and that is precisely what makes `runId!` tempting. But `enabled` is not a type guard —
 * the compiler cannot see it — and it is not a runtime guarantee either: `refetch()` ignores
 * `enabled` and calls `queryFn` regardless. So the null branch below is a real, reachable state, and
 * it is handled by narrowing rather than asserted away.
 */
export function useTailoringRun(runId: string | null): ReturnType<typeof useQuery<TailoringRun>> {
  return useQuery({
    queryKey: tailoringRunQueryKey(runId),
    queryFn: ({ signal }) => {
      if (runId === null) {
        // Reached only through `refetch()` on the disabled query. Rejecting puts the query in its
        // error state rather than issuing a request to `/api/tailoring-runs/null`.
        return Promise.reject(new Error('useTailoringRun: there is no run to fetch.'));
      }
      return fetchTailoringRun(runId, signal);
    },
    enabled: runId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status !== undefined && isActiveTailoringRunStatus(status) ? POLL_INTERVAL_MS : false;
    },
    refetchIntervalInBackground: false,
  });
}
