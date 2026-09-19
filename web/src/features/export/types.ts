/**
 * Mirrors `CreateExportRequest` / `ExportJobResponse` / `ExportJobListResponse` in the API's
 * `infrastructure/api/schemas/export.py`, field for field, nullability included.
 *
 * Hand-written for now, and that is a known seam (`features/intake/types.ts`,
 * `features/posting/types.ts` and `features/tailoring/types.ts` all carry the same note): two
 * declarations of one contract drift. When the API surface grows past a couple of endpoints,
 * generate these from the OpenAPI schema FastAPI already publishes at `/openapi.json` rather than
 * maintaining them by hand.
 *
 * Wire types, not view models: UUIDs are `string`, datetimes are ISO-8601 `string`s, and every
 * field is `readonly` because a response is a fact the server stated, not something the client
 * edits. The *view* — what a control says and offers — is `exportView.ts`'s job, derived from
 * these; nothing here is shaped for a screen.
 */

import type { TailoredDocumentKind } from '@/features/tailoring/types';

/**
 * One of the four formats a document can leave in. Mirrors the domain's `ExportFormat` StrEnum —
 * a closed set of four, so a union of literals rather than `string`: an unhandled member is a
 * compile error, not a silent fallthrough.
 *
 * **The values are wire spellings**: a query parameter (`?format=md`), a JSON field on the job
 * resource and a file extension in the server's storage grammar are all the same string, on the
 * server by construction. Keeping them identical here means there is no mapping table on this side
 * either — and so nothing that can drift out of step with the one on the other.
 */
export type ExportFormat = 'md' | 'txt' | 'pdf' | 'docx';

/**
 * The two formats a worker renders (ADR-0005: the cost of the work, not the tidiness of treating
 * all four alike). Requesting one creates an `ExportJob` row, which is polled and then downloaded.
 *
 * A separate type rather than a `delivery` lookup, because the split is a property of the
 * *endpoint*: `POST /api/tailoring-runs/{id}/exports` types its `format` as `Literal["pdf",
 * "docx"]` on the server, so `{"format": "md"}` is FastAPI's own 422 before a handler runs. This
 * union is that same lock one layer out — the request is unconstructable, not merely rejected.
 */
export type QueuedExportFormat = 'pdf' | 'docx';

/**
 * The two formats the API renders inside the request, because rendering them is string
 * manipulation. **There is no job, no row, no poll and no file** — the bytes come back from
 * `GET /api/tailoring-runs/{id}/documents/{kind}/download?format=md` and nothing is written.
 *
 * That is why the per-format view for an inline format has three states and not nine: there is no
 * server-side lifecycle to reflect, only the client's own download.
 */
export type InlineExportFormat = 'md' | 'txt';

/**
 * Where an export job stands. Mirrors the domain's `ExportJobStatus` — four values, closed.
 *
 * `queued` and `rendering` are the two non-terminal states the poller keeps polling through;
 * `ready` and `failed` are terminal. **A `failed` job arrives as a 200** on the same polled
 * resource as a `rendering` one — it is a recorded state of the job (ADR-0014), not an HTTP error,
 * which is why the UI must tell the two apart by `status` and never by the response code.
 */
export type ExportJobStatus = 'queued' | 'rendering' | 'ready' | 'failed';

/**
 * Why a job ended in `failed`. Mirrors the domain's `ExportFailureReason` — nine values, closed.
 *
 * A component maps these to copy with an exhaustive `Record<ExportFailureReason, …>`, so a tenth
 * reason added on the server becomes a TypeScript error at the one place that must write a sentence
 * for it, rather than a blank box in the browser.
 *
 * What these reasons do **not** decide here is whether *Export again* is offered. That is
 * `retryable`, below, and it comes from the API (Constitution §4.5).
 */
export type ExportFailureReason =
  | 'render_failed'
  | 'render_timed_out'
  | 'output_too_large'
  | 'file_store_unavailable'
  | 'source_changed'
  | 'source_unavailable'
  | 'not_queued'
  | 'abandoned'
  | 'render_error';

/**
 * The request body for `POST /api/tailoring-runs/{run_id}/exports` — the whole of it.
 *
 * **Which run** is in the URL, not here: the run is the resource this collection hangs off, and a
 * run id in the body would be a second copy of it free to disagree with the first. The Pydantic
 * model is `extra="forbid"`, so a stray field is a 422.
 */
export interface NewExport {
  readonly document: TailoredDocumentKind;
  readonly format: QueuedExportFormat;
}

/**
 * One `ExportJob` as the client sees it — the body of the `POST` (202, or 200 when an identical
 * request already made this job), of `GET /api/export-jobs/{id}`, and of every row of the listing.
 * One shape for all three, so there is one parser and one renderer.
 *
 * **This body carries no PII and no path** (ADR-0016 (e)): no `file_key`, no filesystem path and
 * not one character of the document. The bytes live behind `file_url`, which is a second authorized
 * request.
 */
export interface ExportJob {
  readonly id: string;
  readonly tailoring_run_id: string;

  readonly document: TailoredDocumentKind;
  readonly format: ExportFormat;

  readonly status: ExportJobStatus;
  readonly failure_reason: ExportFailureReason | null;

  /**
   * Whether *Export again* is worth offering. **Computed by the API from `failure_reason`; the
   * client reads it and never re-derives it** (AC-24, Constitution §4.5). "Which failures are worth
   * asking again for" is a business rule, and a TypeScript copy of it would be a second authority
   * that drifts — `render_failed`, `output_too_large` and `source_unavailable` are `false` because
   * the same input would fail the same way.
   *
   * `false` for a job that has not failed, which is not a claim that it is unretryable — read it
   * only when `status === 'failed'`.
   */
  readonly retryable: boolean;

  /** The run version this job was requested at. */
  readonly run_version: number;

  /**
   * Whether `run_version` is still the run's version — i.e. whether this file is the document as it
   * now stands. **A cross-aggregate comparison neither aggregate can make alone**, so the server
   * makes it and the client reads it; `false` also when the run is gone entirely. This is what
   * chooses between *Download* and *Export again*, and the file itself is still served for a stale
   * ready job, because it is the user's own.
   */
  readonly current: boolean;

  readonly byte_size: number | null;
  readonly render_duration_ms: number | null;

  /**
   * **`null` until `ready`**, and a path rather than an absolute URL (`/api/export-jobs/{id}/file`):
   * the client is same-origin and the typed client prepends nothing. Building this string in
   * TypeScript would put the URL grammar in two places.
   *
   * It is never an `<a href>`. An anchor that 401s downloads a JSON error body under a `.pdf` name;
   * the download goes through the typed client and arrives as a `Blob` (AC-42).
   */
  readonly file_url: string | null;

  readonly requested_at: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;

  /** The **guest session's** expiry — the session owns the 24-hour promise (ADR-0006). */
  readonly expires_at: string;
}

/**
 * Every `ExportJob` requested for one run, newest first. `items` is `[]` for a run nobody has
 * exported — an ordinary answer, never a 404, and it is what the export bar renders on first paint.
 *
 * A run has two documents and two queued formats, so four in flight is the realistic maximum and
 * the reason this is a list rather than a lookup: a browser refresh loses every job id the page was
 * holding, and one request keyed on the run — which *is* in the URL — reattaches to all of them.
 */
export interface ExportJobListResponse {
  readonly items: readonly ExportJob[];
}

/**
 * Whether a job is still in flight — `queued` or `rendering` — and so worth polling.
 *
 * This restates the server's own documented polling contract (the two non-terminal members of
 * `ExportJobStatus`), not a business rule the client invents. It lives here, beside the union it
 * reads, so the poller and whatever else asks "is this the working state" cannot disagree about
 * which statuses count — the call `features/tailoring/types.ts` made for
 * `isActiveTailoringRunStatus`. Written as a `switch` over the union so a fifth status is a compile
 * error rather than a job the poller silently abandons.
 */
export function isActiveExportStatus(status: ExportJobStatus): boolean {
  switch (status) {
    case 'queued':
    case 'rendering':
      return true;
    case 'ready':
    case 'failed':
      return false;
  }
}
