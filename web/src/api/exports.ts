import { request, requestBlob } from './client';

import type {
  ExportJob,
  ExportJobListResponse,
  InlineExportFormat,
  NewExport,
} from '@/features/export/types';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

/**
 * The export endpoints — **two resource shapes over one router**, and the split is visible here.
 *
 * A job is created and listed under its run (`/api/tailoring-runs/{id}/exports`, because the run is
 * what a browser refresh still knows) and then polled and downloaded by its own id
 * (`/api/export-jobs/{id}`, flat, because a nested URL would make every poll re-prove a link the
 * job row already holds). `fetchExportJobs` and `fetchExportJob` therefore build different paths
 * for what is, in the response, the same shape.
 *
 * **All five require a guest session, and none of them mints one** (ADR-0014 §3): a missing,
 * unknown or expired cookie is a 401 `guest_session_expired` on every one, reads included. Each
 * function throws that as an `ApiError` like any other failure; what it *means* for the UI is
 * decided one layer up.
 *
 * Two of the five answer **bytes**, so they go through `requestBlob` rather than `request`. There
 * is no `<a href>` to any of these paths anywhere in the app — see `requestBlob`'s docstring for
 * the reason (AC-42).
 */

/**
 * Every export job the run has ever been asked for, newest first. `items` is `[]` for a run nobody
 * has exported — an ordinary answer, never a 404.
 *
 * **This is the endpoint the poller reads** (`useExportJobs`), once per second while anything is in
 * flight, for up to four jobs at a time. A `failed` job arrives here as a 200 with `status:
 * 'failed'`, not as a thrown error; the only 4xx are the session's 401 and a 404
 * `tailoring_run_not_found` (identical for "does not exist" and "not yours").
 */
export function fetchExportJobs(
  runId: string,
  signal?: AbortSignal,
): Promise<ExportJobListResponse> {
  return request<ExportJobListResponse>(
    `/api/tailoring-runs/${encodeURIComponent(runId)}/exports`,
    { ...(signal ? { signal } : {}) },
  );
}

/**
 * One export job by its own id — the flat resource, for a caller holding a job and not a run.
 *
 * Nothing in this slice's UI calls it: the bar polls the list, because a refresh needs the list
 * anyway and four pollers for one run is four times the requests. It exists because the `POST`'s
 * `Location` header names this URL and a client that follows it should not have to build the path
 * itself.
 */
export function fetchExportJob(id: string, signal?: AbortSignal): Promise<ExportJob> {
  return request<ExportJob>(`/api/export-jobs/${encodeURIComponent(id)}`, {
    ...(signal ? { signal } : {}),
  });
}

/**
 * Ask for one PDF or DOCX. The API answers **202** with the job already `queued` and `file_url` /
 * `byte_size` `null`; the caller polls the run's export list until it is terminal.
 *
 * **A 200 rather than a 202 means the job already existed** and is returned unchanged — the same
 * (run, document, format) at the same run version, requested twice (X-16). No row was created and
 * no worker was paid. `client.ts` treats every 2xx as success and this function returns the same
 * shape either way, which is the point: the client has one code path, and the status code is left
 * as the honest server-side record of whether a row was created. Nothing here reads it.
 *
 * Rejections arrive as `ApiError`s keyed by `code`: 401 `guest_session_expired`; 404
 * `tailoring_run_not_found`; 409 `tailoring_run_not_exportable` (carries `status`) or
 * `too_many_export_jobs` (carries nothing — the limit is not a number to show); 413
 * `request_too_large`; 422 `validation_error`; 429 `rate_limited` (with `Retry-After`); 503
 * `queue_unavailable` or `service_unavailable`. Note the absence of `rate_limit_unavailable`: this
 * limiter fails open, because a render costs worker seconds and no money.
 */
export function requestExport(
  runId: string,
  body: NewExport,
  signal?: AbortSignal,
): Promise<ExportJob> {
  return request<ExportJob>(`/api/tailoring-runs/${encodeURIComponent(runId)}/exports`, {
    method: 'POST',
    body,
    ...(signal ? { signal } : {}),
  });
}

/**
 * The bytes of a finished export job, as a `Blob`.
 *
 * **The path is built here rather than read from the job's `file_url`**, and the two are the same
 * string by design. `file_url` is not a link the client concatenates onto — what the client reads
 * from it is its *nullness*: it is `null` until the job is `ready`, which is the server saying the
 * bytes exist. The grammar of the URL belongs to whoever answers it, and this function is where
 * this side of the wire writes it down once.
 *
 * Rejections: 401 `guest_session_expired`; 404 `export_job_not_found`; 409 `export_not_ready` (the
 * job is `queued`, `rendering` or `failed` — the body carries `status`, and `failure_reason` when
 * it failed, so the control can return to its polled state without a second request); 410
 * `export_file_gone` (the row says ready and the store has nothing — the answer is *Export
 * again*); 503 `service_unavailable`.
 */
export function downloadExportFile(id: string, signal?: AbortSignal): Promise<Blob> {
  return requestBlob(`/api/export-jobs/${encodeURIComponent(id)}/file`, {
    ...(signal ? { signal } : {}),
  });
}

/**
 * The bytes of one of a `succeeded` run's two documents, rendered to Markdown or plain text
 * **inside the request**, as a `Blob`.
 *
 * There is no job, no row, no file and no poll: rendering these two formats is string manipulation,
 * so it happens on the spot and nothing is written (ADR-0005, AC-8). That is why this is the only
 * download in the app whose result is immediate, and why the two inline controls keep working when
 * the export list query is down — they do not depend on it.
 *
 * `format` is typed as the inline subset, mirroring the query parameter's `Literal["md", "txt"]` on
 * the server: asking for a PDF here is not a rejected request, it is one that does not compile. The
 * document served is the **current** one — the visitor's revision where there is one, else the
 * model's draft.
 *
 * Rejections: 401 `guest_session_expired`; 404 `tailoring_run_not_found`; 409
 * `tailoring_run_not_exportable` (carries `status`); 422 `validation_error`; 500 `render_failed`
 * (deliberate — a valid string the pipeline cannot render is our bug, and a retry will not help);
 * 503 `render_timed_out` or `service_unavailable`.
 */
export function downloadDocument(
  runId: string,
  kind: TailoredDocumentKind,
  format: InlineExportFormat,
  signal?: AbortSignal,
): Promise<Blob> {
  return requestBlob(
    `/api/tailoring-runs/${encodeURIComponent(runId)}/documents/${kind}/download?format=${format}`,
    { ...(signal ? { signal } : {}) },
  );
}
