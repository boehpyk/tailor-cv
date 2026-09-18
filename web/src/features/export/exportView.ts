/**
 * The per-format state machine of the export bar — **declared here, derived in F6.**
 *
 * `viewOfExport` below is a **deliberate stub**: it returns `{ kind: 'idle' }` and contains no
 * derivation whatsoever. Read the docstring on the function before "finishing" it.
 */

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
   * `exportCopy`'s job and F6's work, and doing it at the call site would put that mapping in a
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
   * `!` or a cast tempting at the one line in F6 that reads it.
   *
   * It is ruled out anyway, for the reason **1.3 already settled one context over**: `RunView`'s
   * `failed` takes a nullable `TailoringFailureReason` and `TailoringFailureNotice` renders a
   * generic sentence for `null`, rather than asserting a column constraint from TypeScript. A `!`
   * here would be a client-side claim about a server-side invariant — a second authority that is
   * silently wrong the day the constraint changes (Constitution §4.5), and whose failure mode is
   * *NaN KB* in a stranger's browser. F6's copy degrades instead: no *· 84 KB* clause when the size
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
   * F6's `exportCopy` map does the same — exhaustive over the nine reasons, with one more sentence
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
 * Decide one control's view from the run's export jobs and the client's own two requests. Pure — no
 * hooks, no state, no JSX.
 *
 * ---
 *
 * # THIS IS A STUB, ON PURPOSE. DO NOT IMPLEMENT IT HERE.
 *
 * It returns `{ kind: 'idle' }` for every input, and that is the entire content of task F3. The
 * derivation is **F6**, written to make `qa`'s F5 tests go from red to green.
 *
 * The reason is a lesson this project has now paid for four times. A SKELETON exists so that a test
 * fails **on its assertion** rather than on an `ImportError` — a red that proves a file is absent
 * proves nothing about whether the assertion discriminates (docs/sdlc.md §2). Slices 1.1 (T33/T34),
 * 1.3 (T40) and 1.4 (F5/F8) each shipped a frontend skeleton that **already worked**, so the tests
 * written against it passed the moment they arrived. A test that has never been observed failing is
 * a test nobody has checked, and every one of those bought exactly nothing.
 *
 * So: the types below and above are real, because **a type is data** — a union's member list is a
 * field list, like a frozen dataclass's, and there is no body in it to defer (the same call
 * `domain/export/events.py` made at T1 and the pipeline's constants made at I1). The *logic* is
 * absent, and F5's table will red on every row but the trivially-idle one.
 *
 * ---
 *
 * ## What F6 implements here
 *
 * Pick the latest job of `jobs` matching `target`, then, in the order the union is declared:
 * a request in flight for this target outranks everything (there is no job yet); a download in
 * flight or a download that rejected is about *this* control only; otherwise the job's `status`
 * chooses, with `ready` splitting on the server's `current` into `ready` and `stale`, and `failed`
 * carrying the server's `failure_reason` and `retryable` through untouched. No job at all, and an
 * inline format in its resting state, are both `idle`.
 *
 * `elapsedSeconds` comes from `nowMs` against `requested_at` (for `queued`) or `started_at` (for
 * `rendering`) — which is what F4's bar ticks once a second with its one `setInterval`, and the
 * reason this function takes a clock reading instead of calling `Date.now()` itself: a function
 * that reads the wall clock cannot be table-tested.
 *
 * ## Signature note — two deviations from the plan, both deliberate
 *
 * The plan writes `viewOfExport(format, jobsForDocument, run, mutations)` and AC-37 writes
 * `viewOfExport(jobs, run, mutations)`. Those two disagree with each other, and neither has
 * anywhere to put "what time is it now", which the two elapsed-seconds members need. So:
 *
 * 1. **`nowMs` is added.** There is no honest way to produce `elapsedSeconds` without it.
 * 2. **`run` is dropped.** The plan's own state table says the bar reads the run "for nothing but
 *    the id (`current` comes from the server)", and that is the point: the one comparison a run
 *    could contribute here is `job.run_version === run.version`, which is exactly the cross-
 *    aggregate judgement the API already made and put in `current` (Constitution §4.5). A `run`
 *    parameter would be a standing invitation to re-derive it in TypeScript — a second authority
 *    that drifts the first time the server's definition of "current" changes. Leaving it out makes
 *    that impossible rather than discouraged.
 *
 * `target` replaces the loose `format` and pre-filtered `jobsForDocument`, so the function does its
 * own picking (as the plan's prose describes: "picks the latest job for (document, format)") and
 * compares one value against the mutations' variables instead of matching two fields by hand.
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
  // Read and discarded. The parameters are real — they are the contract F5's tests call through
  // and F6 derives from — but nothing uses them yet, and `noUnusedParameters` is on. Renaming them
  // to `_target` and friends to silence the compiler would hide the signature from the reader who
  // arrives next; discarding them explicitly says "declared, not yet used" in a way that is
  // impossible to mistake for an oversight. F6 deletes these four lines by using all four values.
  //
  // The rule below is right in general — `void` on something that is not a call discards nothing —
  // and wrong for this one case, which is the only situation it exists to catch and also the only
  // situation where discarding nothing is exactly the intent. Disabled by name, for four lines,
  // with the reason attached, rather than by weakening the rule in `eslint.config.js`.
  /* eslint-disable @typescript-eslint/no-meaningless-void-operator */
  void target;
  void jobs;
  void mutations;
  void nowMs;
  /* eslint-enable @typescript-eslint/no-meaningless-void-operator */

  return { kind: 'idle' };
}
