import { useQuery } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { fetchExportJobs } from '@/api/exports';

import { isActiveExportStatus } from '../types';

import type { ExportJobListResponse } from '../types';

/**
 * The cache key for one run's export jobs — the key the poller owns, and the key a successful
 * document save invalidates (AC-41).
 *
 * Exported because two other files name it: `useRequestExport` invalidates it when a job is
 * created, and `useDocumentAutosave` invalidates it when a `PUT` lands, so that a `ready` job whose
 * `run_version` is now behind reads `current: false` on the next response and its control flips to
 * *stale* without a reload. A key written out by hand in three places is three chances to write
 * `'exports'` in one of them and watch an invalidation quietly do nothing.
 */
export function exportJobsQueryKey(runId: string) {
  return ['export', 'exportJobs', runId] as const;
}

/** How often to re-read the list while any job of this run is `queued` or `rendering`. */
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
 * Watch every export job of one run until they are all finished — **one poller for up to four
 * jobs.**
 *
 * A run has two documents and two queued formats, so four renders can be in flight at once. The
 * obvious shape is `useExportJob(id)` per control, and it is the wrong one here for two reasons.
 * Four observers of four keys is four requests a second for one run's worth of state. And a browser
 * refresh loses every job id the page was holding, so a per-job poller has nothing to reattach to —
 * whereas the run id is in the URL, and one request keyed on it brings back all four. The list is
 * needed on first paint regardless; polling it costs nothing extra.
 *
 * **`refetchInterval` returning `false` is the polling loop**, exactly as in `useTailoringRun`.
 * TanStack re-evaluates the function after every fetch against the state that fetch produced: while
 * some job is `queued` or `rendering` it answers `1000` and the list is re-read a second later; the
 * moment every job is `ready` or `failed`, it answers `false` and there is no next request (AC-39).
 * The loop stops *because the data says it is over*, which is the only thing that should stop it.
 *
 * That is why there is no `useEffect`, no `setInterval` and no cleanup in this file. A hand-rolled
 * timer has to be cleared on every path out — the last job finishing, the unmount, the run id
 * changing, the tab going to the background — and the one path someone forgets is a timer still
 * hitting the API for a result nobody is waiting for. Here the timer belongs to the query observer,
 * which stops it on unmount and on a key change without being asked.
 *
 * `refetchIntervalInBackground: false` covers the hidden tab: it is not polled, and TanStack
 * refetches on focus, so the user sees the current state the moment they come back. A PDF render is
 * worker seconds, not model tokens, but a background tab waking once a second for ten minutes is a
 * battery, and the file will still be there.
 *
 * Before the first response `data` is `undefined`, the interval is `false`, and that is correct:
 * the initial fetch is not driven by the interval, and nothing is known yet to poll for. An empty
 * `items` is the same — a run nobody has exported has nothing in flight, and the bar renders four
 * idle controls (AC-43).
 *
 * **The error state stops the loop before it reads the data.** TanStack keeps the last good `data`
 * after a failed refetch, so a poller looking only at `items` would see `rendering` forever and hit
 * a dead server once a second, with retries, until the tab closed. Two options close that: `retry`
 * skips a 4xx so the error arrives on the first answer rather than after three pointless backoffs,
 * and `refetchInterval` answers `false` whenever the query **is** in error, before it looks at the
 * stale data at all. Transient failures keep their retries — while one is pending the query holds
 * its previous state with its last good data, so a network blip mid-render is not the end of the
 * render.
 *
 * A 401 or a 404 here is not this hook's problem to explain: the run page's own handling takes the
 * whole workspace down on those, and the export bar is rendered inside it.
 */
export function useExportJobs(runId: string): ReturnType<typeof useQuery<ExportJobListResponse>> {
  return useQuery({
    queryKey: exportJobsQueryKey(runId),
    queryFn: ({ signal }) => fetchExportJobs(runId, signal),
    retry: (failureCount, error) => !isClientError(error) && failureCount < MAX_TRANSIENT_RETRIES,
    refetchInterval: (query) => {
      if (query.state.status === 'error') {
        return false;
      }
      const items = query.state.data?.items;
      return items !== undefined && items.some((job) => isActiveExportStatus(job.status))
        ? POLL_INTERVAL_MS
        : false;
    },
    refetchIntervalInBackground: false,
  });
}
