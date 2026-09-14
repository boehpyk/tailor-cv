import { runReadErrorCopy } from './apiErrorCopy';

import type { PreviewDocument } from './components/TailoredDocumentsPreview';
import type { TailoringFailureReason, TailoringRun, TailoringRunSummary } from './types';

/**
 * What the watched run looks like right now — mutually exclusive, so a discriminated union.
 *
 * A `isLoading` flag beside a `data` field would let the page render "Tailoring with Gemini…" and
 * a failure notice in the same frame, and eventually it would. Each member carries exactly what its
 * branch needs to render, already narrowed, so the JSX never re-checks a nullable field.
 *
 * Lifted out of slice 1.3's `TailorPanel` (F4) so that the workspace's latest-run card and the run
 * page can derive the same view from the same two caches. Pure: no hooks, no state, no JSX.
 */
export type RunView =
  | { readonly kind: 'none' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'working'; readonly runId: string; readonly status: 'queued' | 'running' }
  | {
      readonly kind: 'succeeded';
      readonly tailoredCv: PreviewDocument;
      readonly coverLetter: PreviewDocument;
    }
  | {
      readonly kind: 'failed';
      readonly reason: TailoringFailureReason | null;
      readonly retryable: boolean;
    }
  | { readonly kind: 'unreadable'; readonly message: string; readonly canCheckAgain: boolean };

/**
 * Decide the watched run's view from the two caches that know about it. Pure — no hooks, no state.
 *
 * - **An error outranks data.** After a 404 mid-poll TanStack still holds the last `running`
 *   response; rendering that would show "Tailoring with Gemini…" over a run that is gone, with the
 *   poller already stopped and nothing left to correct it.
 * - **The detail outranks the summary.** The detail query is the one being polled; the list's
 *   summary is a snapshot from when the list loaded. The summary is used only until the detail's
 *   first response arrives, and only if it describes *this* run — a run chosen from a 409 is not in
 *   the list.
 * - **A summary can say `succeeded` but cannot show it**: the list omits the documents on purpose,
 *   so that case waits for the detail.
 */
export function viewOfWatchedRun(
  runId: string | null,
  run: TailoringRun | undefined,
  runError: Error | null,
  summary: TailoringRunSummary | null,
): RunView {
  if (runId === null) {
    return { kind: 'none' };
  }
  if (runError !== null) {
    return { kind: 'unreadable', ...runReadErrorCopy(runError) };
  }

  const known = run ?? (summary?.id === runId ? summary : undefined);
  if (known === undefined) {
    return { kind: 'loading' };
  }

  switch (known.status) {
    case 'queued':
    case 'running':
      return { kind: 'working', runId, status: known.status };
    case 'failed':
      return { kind: 'failed', reason: known.failure_reason, retryable: known.retryable };
    case 'succeeded':
      if (run === undefined) {
        return { kind: 'loading' };
      }
      if (run.tailored_cv === null || run.cover_letter === null) {
        // A succeeded run always carries both documents; the wire type is nullable only because it
        // is shared with unfinished runs. Handled as "could not read the run", not asserted away.
        return { kind: 'unreadable', message: "We couldn't load that run.", canCheckAgain: false };
      }
      return {
        kind: 'succeeded',
        tailoredCv: { text: run.tailored_cv, characterCount: run.tailored_cv_character_count },
        coverLetter: { text: run.cover_letter, characterCount: run.cover_letter_character_count },
      };
  }
}
