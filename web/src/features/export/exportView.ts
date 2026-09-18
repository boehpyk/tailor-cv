/**
 * The per-format state machine of the export bar — the pure derivation the whole bar is built on.
 *
 * `viewOfExport` at the bottom of this file takes the run's export jobs (server state, held by
 * TanStack Query), what this browser's own two requests are doing, and a clock reading, and returns
 * one of nine mutually exclusive states. It holds nothing, fetches nothing and reads no wall clock,
 * which is what lets AC-37's table drive it row by row.
 */

import { downloadFailureCopyFor } from './exportCopy';

import type { ExportFailureReason, ExportFormat, ExportJob } from './types';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

/**
 * What one control acts on: one document, one format. The bar renders four of these for the visible
 * document, and every job, request and download in this feature is keyed on such a pair.
 *
 * A named type rather than two loose parameters, because the same pair is also what both mutations
 * carry in their `variables` — so "is *this* control the one that is requesting" is a comparison of
 * two values of one type, not four fields lined up by hand in the right order.
 */
export interface ExportTarget {
  readonly document: TailoredDocumentKind;
  readonly format: ExportFormat;
}

/**
 * What the client's own two requests are doing — the half of the view that no server response
 * knows about.
 *
 * *requesting*, *downloading* and *downloadFailed* are facts about requests this browser made, held
 * by the two mutations; everything else in `ExportView` is a fact the server stated. Passing them in
 * as plain data rather than reading the mutations inside `viewOfExport` is what keeps that function
 * pure and table-testable (AC-37).
 *
 * Each field carries an `ExportTarget`, because one `useRequestExport` and one `useDownload` sit
 * behind four controls: `isPending` alone would put all four into the same state at once. This is
 * the mutations' `variables`, narrowed to what the derivation needs.
 */
export interface ExportMutations {
  /** `useRequestExport`'s variables while its `POST` is in flight, else `null`. */
  readonly requesting: ExportTarget | null;
  /** `useDownload`'s variables while a blob fetch is in flight, else `null`. */
  readonly downloading: ExportTarget | null;
  /**
   * The last download that rejected — what it was for, and why. `null` when none has, or when a
   * later download succeeded.
   *
   * The raw `Error` rather than a finished sentence: turning an `ApiError`'s `code` into copy (401
   * → the session-expired line, 410 `export_file_gone` → *that file is no longer available*, …) is
   * `exportCopy`'s job, and doing it at the call site would put that mapping in a
   * component instead of in the pure function AC-37 asks to be table-tested.
   */
  readonly downloadFailure: { readonly target: ExportTarget; readonly error: Error } | null;
}

/**
 * What one format's control shows right now — nine mutually exclusive states, so a discriminated
 * union (`runView.ts`'s `RunView` is the same call one context over).
 *
 * A `isLoading` boolean beside a `job` field would let a control render *Preparing your PDF…* and
 * *Download PDF* in the same frame, and eventually it would. Each member carries exactly what its
 * branch needs — but **only what the wire can actually guarantee**: two of those fields stay
 * nullable, because narrowing them here would be this function inventing a fact rather than
 * carrying one (see `ready.byteSize` and `failed.reason` below).
 *
 * **The transitions are the server's.** The client never moves a view from `rendering` to `ready`;
 * the next poll does. What the client owns is `requesting`, `downloading` and `downloadFailed`,
 * which are facts about its own requests. That division is the whole reason this is a derivation
 * over a query cache plus two mutations rather than a state machine with its own transitions:
 * there is nothing here for the client to decide.
 *
 * **For an inline format the machine is three states** — `idle`, `downloading`, `downloadFailed` —
 * because Markdown and plain text are rendered inside the request and leave no job to be queued,
 * rendered, ready, stale or failed. One union covers both, rather than two: the six queued-only
 * members are simply unreachable for `md` and `txt`, and a second union would double every
 * component signature to express an absence.
 */
export type ExportView =
  /** No job for this (document, format), or the latest one is spent — *Export PDF* / *Export again*. */
  | { readonly kind: 'idle' }
  /** This control's `POST` is in flight. Not yet a job: nothing has been queued. */
  | { readonly kind: 'requesting' }
  /** A job exists and no worker has picked it up — *Waiting for a worker…* */
  | { readonly kind: 'queued'; readonly elapsedSeconds: number }
  /** A worker is rendering it — *Preparing your PDF… 3s* */
  | { readonly kind: 'rendering'; readonly elapsedSeconds: number }
  /**
   * The file exists and is the document as it stands — *Download PDF · 84 KB*.
   *
   * **`byteSize` is nullable, and that is the wire's doing, not this union's.** One Pydantic model
   * (`ExportJobResponse`) serves all four statuses, so `byte_size` is `number | null` on *every*
   * response, a `ready` one included. The database does guarantee it there — `export_job` carries a
   * `CHECK` that a `ready` row has a byte size (XJ-3) — and that guarantee is exactly what makes a
   * `!` or a cast tempting at the one line in `exportCopy` that reads it.
   *
   * It is ruled out anyway, for the reason **1.3 already settled one context over**: `RunView`'s
   * `failed` takes a nullable `TailoringFailureReason` and `TailoringFailureNotice` renders a
   * generic sentence for `null`, rather than asserting a column constraint from TypeScript. A `!`
   * here would be a client-side claim about a server-side invariant — a second authority that is
   * silently wrong the day the constraint changes (Constitution §4.5), and whose failure mode is
   * *NaN KB* in a stranger's browser. The copy degrades instead: no *· 84 KB* clause when the size
   * is `null`, and the download is offered either way, because the bytes do not depend on knowing
   * how many there are.
   */
  | { readonly kind: 'ready'; readonly jobId: string; readonly byteSize: number | null }
  /**
   * The file exists but the run has moved on since it was made (`current: false`) — *Your document
   * changed — Export again*. The file is still downloadable; it is simply of an older version.
   */
  | { readonly kind: 'stale'; readonly jobId: string }
  /** A blob fetch is in flight for this control. */
  | { readonly kind: 'downloading' }
  /**
   * The job failed. `reason` chooses the sentence and `retryable` decides whether *Export again* is
   * offered — **both read from the server**, never re-derived here (Constitution §4.5).
   *
   * **`reason` is nullable for the same reason `ready.byteSize` is**: `failure_reason` is
   * `ExportFailureReason | null` on every `ExportJobResponse`, whatever its status, because one
   * schema serves all four. The `CHECK` on a `failed` row makes the `null` unreachable in practice
   * and that is precisely the trap — an unreachable branch asserted away with `!` is an unreachable
   * branch until someone adds a fifth status.
   *
   * The precedent is literal here rather than analogous: `RunView`'s `failed` is
   * `TailoringFailureReason | null` and `failureCopyFor(null)` already returns a generic headline.
   * `exportCopy`'s map does the same — exhaustive over the nine reasons, with one more sentence
   * for `null` — so the user of a job that failed for a reason nobody can name still reads a
   * sentence rather than an empty box.
   */
  | {
      readonly kind: 'failed';
      readonly reason: ExportFailureReason | null;
      readonly retryable: boolean;
    }
  /** The blob fetch rejected — *Couldn't download* with *Try again*, on this control only. */
  | { readonly kind: 'downloadFailed'; readonly message: string };

/**
 * The most recent job for one (document, format), or `undefined` when there is none.
 *
 * **The list is newest-first**, as the API documents it, so "latest" is the first match and not a
 * scan for the largest `requested_at`. Sorting here would be this client re-deciding an order the
 * server already chose, and the two would disagree the first time two jobs shared a second.
 *
 * Exported because `ExportBar` needs the same job to decide what a click *does* — download the file
 * or pay for a new render — and a second copy of this filter in the container is a second answer to
 * "which job is this control about".
 */
export function latestExportJobFor(
  target: ExportTarget,
  jobs: readonly ExportJob[],
): ExportJob | undefined {
  return jobs.find((job) => job.document === target.document && job.format === target.format);
}

/** Whether a mutation's variables are about this control. `null` (nothing in flight) never is. */
function isSameTarget(candidate: ExportTarget | null, target: ExportTarget): boolean {
  return (
    candidate !== null &&
    candidate.document === target.document &&
    candidate.format === target.format
  );
}

/**
 * Whole seconds between a server timestamp and the client's clock, never negative.
 *
 * The clamp is not decoration: these are two different machines' clocks, and a browser a few
 * seconds behind the server would otherwise render *Preparing your PDF… -2s*. Flooring rather than
 * rounding means the count reads 0s for the first second, which is what a stopwatch does.
 */
function elapsedSecondsSince(isoTimestamp: string, nowMs: number): number {
  return Math.max(0, Math.floor((nowMs - Date.parse(isoTimestamp)) / 1000));
}

/**
 * Decide one control's view from the run's export jobs and the client's own two requests. Pure — no
 * hooks, no state, no JSX, no `Date.now()`.
 *
 * ## The order of the branches is the specification
 *
 * Three of the nine states are facts about *this browser* and outrank anything the server has said,
 * because the server does not know about them yet:
 *
 * 1. **`requesting`** — the `POST` is in flight, so whatever job the last poll returned is about to
 *    be replaced. A control that kept showing *failed* under the click that is re-requesting it
 *    reads as "the click did nothing".
 * 2. **`downloading`** — a blob fetch is in flight for this control, over a `ready` job that is
 *    still perfectly ready. This is the one place the view deliberately hides a true server fact,
 *    because the user's question right now is "did my click work", not "does the file exist".
 * 3. **`downloadFailed`** — the last blob fetch for this control rejected. With one exception: a
 *    409 `export_not_ready` is a lost race rather than an error (`downloadFailureCopyFor` returns
 *    `null` for it and the docstring there explains why), and the control falls through to the
 *    job's own polled state.
 *
 * Everything below that is the server's, carried through untouched: `status` chooses the member,
 * `ready` splits on the server's `current` into `ready` and `stale`, and `failed` passes on
 * `failure_reason` and `retryable` exactly as sent (Constitution §4.5 — the client never re-derives
 * either). **The client never moves a view from `rendering` to `ready`; the next poll does.**
 *
 * For an inline format (`md`, `txt`) no job can match, because a job row only ever exists for `pdf`
 * and `docx` — so the six queued-only members are simply unreachable and the machine is the
 * three-state one the plan describes, without a second union to say so.
 *
 * ## Signature note — two deviations from the plan, both deliberate
 *
 * The plan writes `viewOfExport(format, jobsForDocument, run, mutations)` and AC-37 writes
 * `viewOfExport(jobs, run, mutations)`. Those two disagree with each other, and neither has
 * anywhere to put "what time is it now", which the two elapsed-seconds members need. So:
 *
 * 1. **`nowMs` is added.** There is no honest way to produce `elapsedSeconds` without it, and a
 *    function that reads the wall clock itself cannot be table-tested.
 * 2. **`run` is dropped.** The one comparison a run could contribute here is
 *    `job.run_version === run.version`, which is exactly the cross-aggregate judgement the API
 *    already made and shipped as `current`. A `run` parameter would be a standing invitation to
 *    re-derive it in TypeScript; leaving it out makes that impossible rather than discouraged.
 *
 * @param target which document and format this control acts on
 * @param jobs the run's export jobs, as `useExportJobs` holds them — all documents, all formats
 * @param mutations what this browser's own request and download are doing
 * @param nowMs the current time in epoch milliseconds, ticked by the bar
 */
export function viewOfExport(
  target: ExportTarget,
  jobs: readonly ExportJob[],
  mutations: ExportMutations,
  nowMs: number,
): ExportView {
  if (isSameTarget(mutations.requesting, target)) {
    return { kind: 'requesting' };
  }
  if (isSameTarget(mutations.downloading, target)) {
    return { kind: 'downloading' };
  }

  const failure = mutations.downloadFailure;
  if (failure !== null && isSameTarget(failure.target, target)) {
    const message = downloadFailureCopyFor(failure.error);
    // `null` is the 409 `export_not_ready` race: not this control's error to show, so it falls
    // through to the job's own state below rather than being reported as a download failure.
    if (message !== null) {
      return { kind: 'downloadFailed', message };
    }
  }

  const job = latestExportJobFor(target, jobs);
  if (job === undefined) {
    return { kind: 'idle' };
  }

  switch (job.status) {
    case 'queued':
      return { kind: 'queued', elapsedSeconds: elapsedSecondsSince(job.requested_at, nowMs) };
    case 'rendering':
      // `started_at` is set the moment a worker picks the job up, and the wire types it nullable
      // because one schema serves all four statuses. Falling back to `requested_at` keeps the
      // count honest (it over-reports, never under-reports) instead of asserting the column.
      return {
        kind: 'rendering',
        elapsedSeconds: elapsedSecondsSince(job.started_at ?? job.requested_at, nowMs),
      };
    case 'ready':
      return job.current
        ? { kind: 'ready', jobId: job.id, byteSize: job.byte_size }
        : { kind: 'stale', jobId: job.id };
    case 'failed':
      return { kind: 'failed', reason: job.failure_reason, retryable: job.retryable };
  }
}
