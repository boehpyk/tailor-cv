import { queryOptions, useQuery } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { fetchTailoringRun } from '@/api/tailoringRuns';

import { isActiveTailoringRunStatus } from '../types';

import type { TailoringRun } from '../types';

/** The cache key for one run. `null` is a legal member: it is the key of the disabled query. */
export function tailoringRunQueryKey(runId: string | null) {
  return ['tailoring', 'tailoringRun', runId] as const;
}

/**
 * The key and the fetcher for one run, and nothing about *when* to fetch — for a caller that
 * reads or refreshes the run outside this poller. The editor's autosave is the one such caller
 * (1.4): it observes the key so the run stays cached for as long as a document is open, and
 * re-reads it through `queryClient.query(...)` after a 409 to compare texts (AC-33). Polling,
 * retry policy and the 4xx stop stay `useTailoringRun`'s alone; sharing only the key and the
 * fetcher is what keeps two readers of one resource from disagreeing about what it is.
 */
export function tailoringRunQueryOptions(runId: string) {
  return queryOptions({
    queryKey: tailoringRunQueryKey(runId),
    queryFn: ({ signal }) => fetchTailoringRun(runId, signal),
  });
}

/** How often to re-read a run that is still `queued` or `running`. */
const POLL_INTERVAL_MS = 1000;

/**
 * How many times a transient failure is retried before the query settles into its error state.
 * TanStack's own default, written down because `retry` below is a function, so the default no
 * longer applies by itself.
 */
const MAX_TRANSIENT_RETRIES = 3;

/**
 * A 4xx `ApiError` is an answer, not a blip: the server will say the same thing on every attempt.
 * Anything else (a network failure, a 5xx, an unparseable body from a proxy) may well succeed a
 * moment later.
 */
function isClientError(error: Error): boolean {
  return error instanceof ApiError && error.status >= 400 && error.status < 500;
}

/**
 * Watch one tailoring run until it finishes — the poller.
 *
 * **`refetchInterval` returning `false` is the polling loop.** TanStack Query re-evaluates the
 * function after every fetch, against the state that fetch produced. While the run is `queued` or
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
 * the initial fetch is not driven by the interval, and nothing is known yet to poll for.
 *
 * **A 4xx ends the watch too — not only a terminal status.** AC-30 was written about `succeeded`
 * and `failed`, but its intent is broader: *no timer outlives a run that is gone.* A run can be gone
 * without ever reaching a terminal status — a 404 `tailoring_run_not_found` because the guest purge
 * deleted it mid-poll, a 401 `guest_session_expired` because the session lapsed. TanStack keeps the
 * last good `data` after a failed refetch, so a poller that looked only at `data.status` would see
 * `running` forever and hit the API once a second, with retries, until the user closed the tab. Two
 * options close that:
 *
 * - `retry` skips a 4xx `ApiError`, so the error state arrives on the first answer instead of after
 *   three pointless backoffs;
 * - `refetchInterval` answers `false` whenever the query **is** in its error state, before it looks
 *   at the (stale) data at all.
 *
 * Transient failures keep their retries: while a retry is pending the query stays in its previous
 * state with its last good data, so a network blip mid-run is not the end of the run. If the
 * retries run out, the query settles into its error state and the interval stops there as well —
 * the panel then says it lost contact and offers a free "check again", rather than leaving a
 * one-second timer pointed at a server that is down.
 *
 * Note that an option set here **overrides** the `QueryClient`'s default: a client configured with
 * `retry: false` (the test helper is) still gets this function for this query.
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
    retry: (failureCount, error) => !isClientError(error) && failureCount < MAX_TRANSIENT_RETRIES,
    refetchInterval: (query) => {
      if (query.state.status === 'error') {
        return false;
      }
      const status = query.state.data?.status;
      return status !== undefined && isActiveTailoringRunStatus(status) ? POLL_INTERVAL_MS : false;
    },
    refetchIntervalInBackground: false,
  });
}
