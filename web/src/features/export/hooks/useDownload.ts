import { useMutation } from '@tanstack/react-query';

import { saveBlob } from '../saveBlob';

import type { ExportFormat } from '../types';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

/**
 * One download, as the bar asks for it.
 *
 * `fetchBlob` is a thunk rather than a URL because the two kinds of download are different
 * requests: an inline format is `downloadDocument(runId, kind, format)` and a queued one is
 * `downloadExportFile(jobId)`. Closing over the arguments at the call site keeps this hook ignorant
 * of both — it knows only "call this, save what comes back" — and keeps the URL grammar in
 * `src/api/`, which is where it belongs.
 *
 * `document` and `format` are carried so the bar can tell **which control** is downloading. One
 * mutation sits behind four controls, so `isPending` alone would put every control into
 * *downloading* at once; `mutation.variables` says which one, exactly as `useRequestExport`'s do
 * for *requesting*. They are not used by the mutation itself, and that is fine: variables are the
 * mutation's record of what was asked for, which is precisely the question being answered.
 */
export interface DownloadRequest {
  readonly document: TailoredDocumentKind;
  readonly format: ExportFormat;
  /** The name the file lands under — a constant keyed on (document, format). */
  readonly filename: string;
  /** Fetch the bytes. Rejects with an `ApiError` the caller renders as a state. */
  readonly fetchBlob: () => Promise<Blob>;
}

/**
 * Fetch a file and save it — **one mutation for both the inline downloads and the export-job
 * files**, because from the client's side they are the same two steps in the same order.
 *
 * The DOM side effect lives inside `mutationFn`, not in a `useEffect` watching for a blob to
 * appear in state (`saveBlob`'s docstring has the argument). That also means the `Blob` is a local
 * in a promise chain and never a piece of React state: it is not rendered, it does not survive the
 * function, and holding a stranger's CV in a component's state so that an effect could see it
 * would be storing PII for no reason.
 *
 * **Every failure arrives as a rejection with a `code`**, which is what makes the download states
 * renderable: a 401 is the session-expired copy, a 404 is *we couldn't find that file*, a 409
 * `export_not_ready` returns the control to whatever the poller says it is, a 410
 * `export_file_gone` is *that file is no longer available — Export again*, and anything else is
 * *Couldn't download* with *Try again* (AC-42). None of them is a file in the Downloads folder
 * with the wrong contents.
 *
 * `mutationFn` returns `Promise<void>`: there is nothing to put in the cache. A saved file is not
 * server state, and `data` on this mutation would be a value nobody could use.
 */
export function useDownload(): ReturnType<typeof useMutation<void, Error, DownloadRequest>> {
  return useMutation({
    mutationFn: ({ fetchBlob, filename }: DownloadRequest) =>
      fetchBlob().then((blob) => {
        saveBlob(blob, filename);
      }),
  });
}
