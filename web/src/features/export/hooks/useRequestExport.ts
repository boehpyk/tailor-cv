import { useMutation, useQueryClient } from '@tanstack/react-query';

import { requestExport } from '@/api/exports';

import { exportJobsQueryKey } from './useExportJobs';

import type { ExportJob, NewExport } from '../types';

/**
 * Ask for one PDF or DOCX, and let the poller find out what became of it.
 *
 * **`onSuccess` invalidates; it does not `setQueryData`** — `useCreateJobPosting`'s reasoning,
 * applied to a response that is *one item of a list*. Writing the returned job into the list cache
 * by hand would mean deciding, in this file, whether it is a new row to prepend or an existing one
 * to replace: the API answers 202 for the first and 200 for the second (X-16, a re-request at the
 * same run version returns the job unchanged, with no row created and no worker paid), and
 * `client.ts` deliberately hides that distinction because the client should have one code path.
 * Reconstructing it here to patch a cache would be re-deriving a fact the server already stated —
 * and getting it wrong duplicates a control's state in the bar. Invalidating asks the server the
 * same question the initial load did, so the two can never disagree about what the list is.
 *
 * It also starts the loop. The list comes back holding a `queued` job, `useExportJobs`'s
 * `refetchInterval` sees an active status, and the poll begins — without this hook knowing that a
 * poller exists.
 *
 * **`variables` are `NewExport`, which is exactly `{document, format}`, and that is what the bar
 * reads.** With one mutation behind four controls, "a request is in flight" is not enough to render
 * with: the *requesting* state belongs to one control, and `mutation.variables` says which. The
 * alternative — a mutation per control, or a `useState` holding the pending format — is either four
 * hooks or a second copy of a fact TanStack already holds.
 *
 * `useMutation` has no built-in `AbortSignal` plumbing in this TanStack Query version, so
 * `requestExport`'s optional `signal` is unused here; it exists for callers that have one.
 */
export function useRequestExport(
  runId: string,
): ReturnType<typeof useMutation<ExportJob, Error, NewExport>> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (input: NewExport) => requestExport(runId, input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: exportJobsQueryKey(runId) }),
  });
}
