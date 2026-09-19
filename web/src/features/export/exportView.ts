/**
 * The per-format state machine of the export bar — the pure derivation the whole bar is built on.
 *
 * `viewOfExport` at the bottom of this file takes the run's export jobs (server state, held by
 * TanStack Query), what this browser's own two requests are doing, and a clock reading, and returns
 * one of nine mutually exclusive states. It holds nothing, fetches nothing and reads no wall clock,
 * which is what lets AC-37's table drive it row by row.
 */

import { downloadFailureFor, requestFailureFor } from './exportCopy';

import type { ExportNextAction } from './exportCopy';
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
  /**
   * The last `POST /exports` that rejected — what it was for, and why. `null` when none has.
   *
   * The twin of `downloadFailure`, added at slice 1.5's `/verify` because nothing read
   * `useRequestExport`'s error state at all: five failure-contract rows (X-14, X-18, X-19, X-21,
   * X-22) promised the user a sentence and delivered silence. Three of them create **no row**, so
   * unlike a failed download — where the job is still there to poll — there is no other route by
   * which the user could ever learn what happened.
   */
  readonly requestFailure: { readonly target: ExportTarget; readonly error: Error } | null;
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
  /**
   * The blob fetch rejected — *Couldn't download* with *Try again*, on this control only.
   *
   * **`nextAction` is what makes the retry reachable, and it is on the view rather than re-derived
   * at the click** (`/verify` slice 1.5, MAJOR 2). A 410 `export_file_gone` does not change the job
   * row — the download endpoint deliberately writes nothing when it cannot find the file — so a
   * control that asked the row what a click means went on answering *download this ready file* for
   * ever: the copy read *"That file is no longer available — Export again"*, the button re-issued
   * the identical `GET`, it 410'd again, and a reload put the user back on *Download PDF* → 410. An
   * error state whose only affordance reproduces the error has no failure path at all.
   *
   * So the *meaning* of the click is decided here, in the same pure function and from the same
   * rejection that chose the sentence, and `ExportBar` reads it rather than re-deriving it from
   * `jobs`. `exportCopy`'s `DownloadFailureView` is where the mapping lives, because the 410's
   * sentence literally promises the action ("— Export again") and a sentence and the recovery it
   * promises should not be two decisions in two modules.
   */
  | {
      readonly kind: 'downloadFailed';
      readonly message: string;
      readonly nextAction: ExportNextAction;
    }
  /**
   * The `POST /exports` was refused — this control's own sentence, and a retry only if one could
   * work (X-14, X-18, X-19, X-21, X-22).
   *
   * **The state that was missing entirely**, found at slice 1.5's `/verify`. `useRequestExport`'s
   * error was read nowhere, so a refused request put the control back to its idle label and the
   * click looked like it had not happened. For the three rejections that create no row that silence
   * was permanent, and the obvious response to it — click again — is precisely what the 429 and the
   * per-session cap exist to prevent.
   *
   * It carries `retryable` rather than `downloadFailed`'s `nextAction` because a request's retry has
   * only one possible meaning: ask again. The only question is whether asking again can succeed.
   */
  | {
      readonly kind: 'requestFailed';
      readonly message: string;
      readonly retryable: boolean;
    };

/**
 * The most recent job for one (document, format), or `undefined` when there is none.
 *
 * **The list is newest-first**, as the API documents it, so "latest" is the first match and not a
 * scan for the largest `requested_at`. Sorting here would be this client re-deciding an order the
 * server already chose, and the two would disagree the first time two jobs shared a second.
 *
 * Exported for its table test only, and **no longer for `ExportBar`**. The bar used to call it a
 * second time to decide what a click *does*, which is precisely how the row could be asked one
 * question and the view another and the two could disagree (`/verify` slice 1.5, MAJOR 2: a 410
 * leaves the row `ready`). The bar now reads the view it already rendered, so this filter has one
 * caller — `viewOfExport`, below — and "which job is this control about" has one answer.
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
 *    409 `export_not_ready` is a lost race rather than an error (`downloadFailureFor` returns
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
    const downloadFailure = downloadFailureFor(failure.error);
    // `null` is the 409 `export_not_ready` race: not this control's error to show, so it falls
    // through to the job's own state below rather than being reported as a download failure.
    if (downloadFailure !== null) {
      // Spread rather than copied field by field: the sentence and the `nextAction` it promises are
      // one decision, made in `exportCopy`, and picking them apart here would be the seam where they
      // start to disagree again.
      return { kind: 'downloadFailed', ...downloadFailure };
    }
  }

  // Below the download failure and **above the job**, and the ordering is the substance of the
  // state rather than an implementation detail.
  //
  // Above the job, because this browser's own last request is a fresher fact than whatever the poll
  // last said: a user who has just been refused a new export should read that refusal, not the
  // reason an *older* job of theirs failed half a minute ago. For the three rejections that commit
  // no row there is no competing job at all, which is exactly why they were invisible before.
  //
  // Below `requesting`, which is checked at the top: the moment a retry is in flight the failure is
  // last time's news, and the control says *Starting…* again.
  const refusedRequest = mutations.requestFailure;
  if (refusedRequest !== null && isSameTarget(refusedRequest.target, target)) {
    return { kind: 'requestFailed', ...requestFailureFor(refusedRequest.error) };
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
